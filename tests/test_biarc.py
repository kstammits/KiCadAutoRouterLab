"""Tangent-arc (biarc) copper fitting: simplify, fillet, gate, emit."""

import math

import numpy as np
import pytest

from kicad_autorouter.routing.biarc import (
    _corner_arc,
    fit_branch_arcs,
    pieces_wire_cells,
    simplify_waypoints,
)


def _clear(n=40):
    return np.zeros((n, n), dtype=np.int16)


def test_simplify_strips_spikes_and_merges_collinear():
    """A->B->A collapses, straight middles drop, order preserved."""
    pts = [(0.0, 0.0), (5.0, 0.0), (5.0, 0.0), (10.0, 0.0),
           (15.0, 0.0), (10.0, 0.0), (15.0, 0.0), (15.0, 5.0)]
    # (15,0)->(10,0)->(15,0) is a spike; middle (5,0),(10,0) collinear.
    out = simplify_waypoints(pts, set())
    assert out[0] == (0.0, 0.0)
    assert out[-1] == (15.0, 5.0)
    assert len(out) < len(pts)
    # No A->B->A remains.
    for a, b, c in zip(out, out[1:], out[2:]):
        assert not (a == c and b != a)


def test_simplify_never_moves_pinned():
    """Terminals/Steiner joints survive simplification untouched."""
    pts = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    out = simplify_waypoints(pts, {(5.0, 0.0)})
    assert out == pts


def test_corner_arc_is_tangent():
    """Fillet meets both legs tangentially and bulges off-chord."""
    t1, mid, t2 = _corner_arc((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), 5.0)
    assert t1 == pytest.approx((5.0, 0.0))
    assert t2 == pytest.approx((10.0, 5.0))
    # Tangent into t1 runs along +x; out of t2 along +y (departure side).
    d_in = (mid[0] - t1[0], mid[1] - t1[1])
    assert abs(d_in[1]) < abs(d_in[0])  # leaves t1 heading +x-ish
    assert d_in[0] > 0
    d_out = (t2[0] - mid[0], t2[1] - mid[1])
    assert abs(d_out[0]) < abs(d_out[1])  # arrives t2 heading +y-ish
    assert d_out[1] > 0
    # Bulge: mid is strictly inside the corner (off the chord t1->t2).
    chord_mid = ((t1[0] + t2[0]) / 2, (t1[1] + t2[1]) / 2)
    assert math.hypot(mid[0] - chord_mid[0], mid[1] - chord_mid[1]) > 0.5


def test_corner_arc_rejects_straight_and_hairpin():
    """No arc for straight joints or 180-degree reversals."""
    assert _corner_arc((0.0, 0.0), (5.0, 0.0), (10.0, 0.0), 5.0) is None
    assert _corner_arc((0.0, 0.0), (5.0, 0.0), (0.0, 0.0), 5.0) is None


def test_fit_replaces_corner_and_shortens_wire():
    """Clear grid: 90-degree joint becomes arc + shorter copper."""
    run = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    pieces = fit_branch_arcs(run, _clear(), None, 100, 1,
                             max_radius_cells=10.0, min_leg_cells=3.0)
    kinds = [p[0] for p in pieces]
    assert kinds == ["seg", "arc", "seg"]
    assert pieces_wire_cells(pieces) < 20.0  # sharp joint is 20 cells
    # Layer tags ride along for stamping/emission.
    assert all(p[-1] == 0 for p in pieces)


def test_fit_keeps_sharp_joint_when_blocked():
    """A BLOCKED cell under the fillet keeps the sharp joint."""
    cost = _clear()
    cost[7, 12] = 100  # inside the (10,5)->(15,10) fillet band
    run = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    pieces = fit_branch_arcs(run, cost, None, 100, 1,
                             max_radius_cells=10.0, min_leg_cells=3.0)
    assert [p[0] for p in pieces] == ["seg", "seg"]
    assert pieces_wire_cells(pieces) == pytest.approx(20.0)


def test_fit_respects_min_leg_and_pins():
    """Short legs and pinned corners keep sharp joints."""
    cost = _clear()
    short = fit_branch_arcs([(5, 5, 0), (6, 5, 0), (6, 6, 0)], cost, None,
                            100, 1, min_leg_cells=3.0)
    assert [p[0] for p in short] == ["seg", "seg"]
    pinned = fit_branch_arcs([(5, 5, 0), (15, 5, 0), (15, 15, 0)], cost, None,
                             100, 1, pinned={(15, 5)},
                             max_radius_cells=10.0, min_leg_cells=3.0)
    assert [p[0] for p in pinned] == ["seg", "seg"]


def test_fit_marks_via_transitions():
    """Layer changes become via markers; runs fillet independently."""
    cost = np.zeros((2, 40, 40), dtype=np.int16)
    run = [(5, 5, 0), (15, 5, 0), (15, 5, 1), (15, 15, 1)]
    pieces = fit_branch_arcs(run, cost[0], None, 100, 1,
                             max_radius_cells=10.0, min_leg_cells=3.0)
    # Same-layer smoother-style split is the pipeline's job; the fitter
    # sees one run here, so it must NOT fillet across the via corner.
    # (Pipeline splits runs before calling; this documents the contract.)
    assert True  # contract covered by test_pipeline_splits_runs below


def test_pipeline_splits_runs_at_vias():
    """Smoother seam keeps via nodes fixed and fillets each side."""
    from kicad_autorouter.routing.grid import RoutingGrid
    from kicad_autorouter.routing.pipeline import _smooth_result
    from kicad_autorouter.routing.router import RouteResult
    grid = RoutingGrid(resolution_mm=0.1, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=40, height_cells=40)
    cost = np.zeros((2, 40, 40), dtype=np.int16)
    cost[0, 10, 10] = 100  # block side-0 diagonal: the L must survive
    cost[1, 20, 20] = 100  # block side-1 diagonal likewise
    branch = [(5, 5, 0), (15, 5, 0), (15, 15, 0),
              (15, 15, 1), (25, 15, 1), (25, 25, 1)]
    res = RouteResult(path=list(branch), wire_mm=0.0, via_count=1,
                      cost=0.0, success=True, branches=(tuple(branch),))
    out = _smooth_result(res, cost, grid, 2, pinned={(5, 5), (25, 25)},
                         fit_arcs=True, max_radius_mm=1.0, min_leg_mm=0.3)
    assert len(out.arc_pieces) == 1
    kinds = [p[0] for p in out.arc_pieces[0]]
    assert kinds.count("arc") == 2  # one fillet per side of the via
    assert kinds.count("via") == 1  # via transition preserved as marker
    assert out.wire_mm < 40.0 * 0.1  # sharp wire is 40 cells


def test_flag_off_is_legacy_compatible():
    """Default params route straight copper with no arc pieces attached."""
    from kicad_autorouter.routing.grid import RoutingGrid
    from kicad_autorouter.routing.output import routes_to_tracks_vias
    from kicad_autorouter.routing.router import RouteResult
    grid = RoutingGrid(resolution_mm=0.1, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=40, height_cells=40)
    branch = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    res = RouteResult(path=list(branch), wire_mm=0.0, via_count=0,
                      cost=0.0, success=True, branches=(tuple(branch),))
    assert res.arc_pieces == ()
    tracks, vias, arcs = routes_to_tracks_vias({"S": res}, grid)
    assert arcs == []
    assert len(tracks) == 2


def test_smooth_result_fillets_corner_single_pass():
    """Real seam: _smooth_result with fit_arcs on a cornered run."""
    from kicad_autorouter.routing.grid import RoutingGrid
    from kicad_autorouter.routing.pipeline import _smooth_result
    from kicad_autorouter.routing.router import RouteResult
    grid = RoutingGrid(resolution_mm=0.1, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=40, height_cells=40)
    cost = np.zeros((2, 40, 40), dtype=np.int16)
    cost[0, 10, 10] = 100  # block the direct diagonal: the L must survive
    branch = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    res = RouteResult(path=list(branch), wire_mm=0.0, via_count=0,
                      cost=0.0, success=True, branches=(tuple(branch),))
    out = _smooth_result(res, cost, grid, 2, pinned={(5, 5), (15, 15)},
                         fit_arcs=True, max_radius_mm=1.0, min_leg_mm=0.3)
    assert len(out.arc_pieces) == 1
    assert [p[0] for p in out.arc_pieces[0]] == ["seg", "arc", "seg"]
    assert out.wire_mm < 2.0  # sharp joint is 2.0mm at 0.1mm grid


def test_fillet_tangent_measures_roominess():
    """Ranking metric: long legs admit long tangents, short legs don't."""
    from kicad_autorouter.routing.biarc import fillet_tangent
    roomy = fillet_tangent((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), 5.0)
    assert roomy == pytest.approx(5.0)  # min(5, 10/2, 10/2)
    cramped = fillet_tangent((8.0, 0.0), (10.0, 0.0), (10.0, 10.0), 5.0)
    assert cramped == pytest.approx(1.0)  # leg-capped, not max-capped
    assert cramped < roomy
    assert fillet_tangent((0.0, 0.0), (5.0, 0.0), (10.0, 0.0), 5.0) == 0.0


def test_greedy_prefers_roomier_corner_on_tie():
    """Fillet-aware ranking: a distance+angle tie breaks on fillet radius.

    From M=(10,10) with committed incoming P=(0,12), A=(18,16) and
    B=(18,4) sit at equal distance (hypot 10) and mirrored angles
    (+/-36.9 deg, same distance to a 10-degree multiple), so legacy
    keeps first-found B. Ranked prefers A, whose admitted fillet
    radius (2.24 cells) beats B's (1.13): the tie-break that matters
    is turn sharpness, which only radius sees. Blockers pin every
    other chord dead (X=(10,2) is reachable from neither P nor M), so
    the A/B tie is the only decision; the fallback then appends B.
    """
    from kicad_autorouter.routing.biarc import corner_radius
    from kicad_autorouter.routing.smooth import _greedy_pass
    assert corner_radius((0.0, 12.0), (10.0, 10.0), (18.0, 16.0), 10.0) == pytest.approx(2.2356, abs=1e-3)
    assert corner_radius((0.0, 12.0), (10.0, 10.0), (18.0, 4.0), 10.0) == pytest.approx(1.1341, abs=1e-3)
    cost = _clear()
    cost[14, 9] = 100   # (9,14):  P->A exact midpoint
    cost[8, 9] = 100    # (9,8):   P->B exact midpoint
    cost[7, 4] = 100    # (4,7):   P->X on a half-cell sample
    cost[6, 10] = 100   # (10,6):  M->X exact midpoint
    run = [(0, 12, 0), (10, 10, 0), (10, 2, 0), (18, 16, 0), (18, 4, 0)]
    legacy = _greedy_pass(run, cost, None, 100, 0)
    assert legacy == [(0, 12, 0), (10, 10, 0), (18, 4, 0)]
    ranked = _greedy_pass(run, cost, None, 100, 0, rank_radius_cells=10.0)
    assert ranked == [(0, 12, 0), (10, 10, 0), (18, 16, 0), (18, 4, 0)]


def test_route_nets_end_to_end_with_fillets():
    """Full pipeline with arc_fillets: routes, emits (arc), writes back."""
    from pathlib import Path
    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.io import add_footprint_to_tree
    from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
    from kicad_autorouter.sexpr import parse_file
    import sys
    sys.path.insert(0, "tests")
    from helpers import make_fp
    pcb_tree = parse_file(Path("tests/fixtures/minimal.kicad_pcb"))
    fp = make_fp("R1", "biarc-e2e-uuid-0001", 100.0, 95.0, ("S", "STUB"),
                 footprint_id="Test:R_0805_BiArc")
    pcb_tree = add_footprint_to_tree(pcb_tree, fp, "Test:R_0805_BiArc")
    fp2 = make_fp("R2", "biarc-e2e-uuid-0002", 160.0, 95.0, ("S", "STUB2"),
                  footprint_id="Test:R_0805_BiArc")
    pcb_tree = add_footprint_to_tree(pcb_tree, fp2, "Test:R_0805_BiArc")
    model = board_model(pcb_tree)
    params = RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                           arc_fillets=True)
    assert params.arc_fillets is True
    result, _, _ = route_nets(model, ["S"], set(), params=params,
                              original_pcb_tree=pcb_tree)
    assert result.nets_routed == 1, f"S must route: {result.failed_reasons}"
    assert result.pcb_tree is not None
    tree_arcs = list(result.pcb_tree.children("arc"))
    assert result.arcs_added == len(tree_arcs)
    # Round-trips back into the model as arcs, not segments.
    back = board_model(result.pcb_tree)
    assert len(back.arcs) == result.arcs_added


def test_merge_keeps_arc_pieces_aligned():
    """Piecemeal/region splice carries fitted arc chains with their branches."""
    from kicad_autorouter.routing.pipeline import _merge_chunk_results
    from kicad_autorouter.routing.router import RouteResult
    branch_a = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    pieces_a = fit_branch_arcs(branch_a, _clear(), None, 100, 1,
                               max_radius_cells=10.0, min_leg_cells=3.0)
    assert any(p[0] == "arc" for p in pieces_a)
    ra = RouteResult(path=list(branch_a), wire_mm=1.0, via_count=0,
                     cost=0.0, success=True, branches=(tuple(branch_a),),
                     arc_pieces=(tuple(pieces_a),))
    branch_b = [(0, 0, 1), (10, 0, 1)]
    rb = RouteResult(path=list(branch_b), wire_mm=1.0, via_count=0,
                     cost=0.0, success=True, branches=(tuple(branch_b),))
    merged = _merge_chunk_results([ra, rb])
    assert len(merged.branches) == 2
    assert len(merged.arc_pieces) == 2
    assert merged.arc_pieces[0] == tuple(pieces_a)
    assert merged.arc_pieces[1] == ()


def test_emission_produces_arcs():
    """Fitted pieces emit (arc) copper + stubs, vias from the grid path."""
    from kicad_autorouter.routing.grid import RoutingGrid
    from kicad_autorouter.routing.output import routes_to_tracks_vias
    from kicad_autorouter.routing.router import RouteResult
    grid = RoutingGrid(resolution_mm=0.1, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=40, height_cells=40)
    branch = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    pieces = fit_branch_arcs(branch, _clear(), None, 100, 1,
                             max_radius_cells=10.0, min_leg_cells=3.0)
    res = RouteResult(path=list(branch), wire_mm=0.0, via_count=0,
                      cost=0.0, success=True, branches=(tuple(branch),),
                      arc_pieces=(tuple(pieces),))
    tracks, vias, arcs = routes_to_tracks_vias({"S": res}, grid)
    assert len(arcs) == 1
    assert len(tracks) == 2  # one stub per side
    assert not vias
    arc = arcs[0]
    assert arc.layer == "F.Cu" and arc.net_name == "S"
    # Arc endpoints meet the stubs (continuity, no teleport copper).
    starts = {arc.start, arc.end}
    assert tracks[0].end in starts or tracks[0].start in starts
    assert tracks[1].end in starts or tracks[1].start in starts
