"""Tangent-arc (biarc) copper fitting: simplify, fillet, gate, emit."""

import math

import numpy as np
import pytest

from kicad_autorouter.routing.biarc import (
    _corner_arc,
    fit_branch_arcs,
    fit_branch_arcs_with_sweeps,
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
    """The fitter never invents via markers; the seam inserts them at splits."""
    run = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    pieces = fit_branch_arcs(run, _clear(), None, 100, 1,
                             max_radius_cells=10.0, min_leg_cells=3.0)
    assert [p[0] for p in pieces] == ["seg", "arc", "seg"]
    assert not any(p[0] == "via" for p in pieces)  # no layer info in a run


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
    """Fillet-aware ranking: a distance tie breaks on fillet radius.

    From M=(10,10) with committed incoming P=(0,12), A=(18,16) and
    B=(18,4) sit at equal distance (hypot 10), so legacy keeps
    first-found B. Ranked prefers A, whose admitted fillet
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


@pytest.mark.minimal
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


@pytest.mark.unit
def test_fit_rejects_foreign_tht_hard_exclusion():
    """Clearance gate: a foreign-THT hit anywhere on the arc kills it."""
    import numpy as np
    # Same L as test_fit_replaces_corner_and_shortens_wire (arc mid ~ (13.5, 6.5)).
    run = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    blocked = np.zeros((40, 40), dtype=bool)
    blocked[6, 14] = True  # inside the fillet band, off both legs
    out = fit_branch_arcs(run, _clear(), None, 100, 1,
                          blocked_layer=blocked, hard_radius=1,
                          max_radius_cells=10.0, min_leg_cells=3.0)
    assert [p[0] for p in out] == ["seg", "seg"]
    # Control: no exclusion, same geometry fillets.
    out = fit_branch_arcs(run, _clear(), None, 100, 1,
                          max_radius_cells=10.0, min_leg_cells=3.0)
    assert [p[0] for p in out] == ["seg", "arc", "seg"]


@pytest.mark.unit
def test_fit_exempts_own_net_soft_mask():
    """Own-pad soft mask: cost hits under it don't kill the fillet."""
    import numpy as np
    run = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    cost = _clear()
    soft = np.zeros((40, 40), dtype=bool)
    # Poison the whole fillet band at penalized (not hard-blocked) level,
    # then exempt it as own copper. Cells >= blocked_threshold are always
    # impassable, so the poison must sit in the soft-exemptible band.
    for rr in range(4, 12):
        for cc in range(9, 17):
            cost[rr, cc] = 80
            soft[rr, cc] = True
    out = fit_branch_arcs(run, cost, soft, 100, 1,
                          max_radius_cells=10.0, min_leg_cells=3.0)
    assert [p[0] for p in out] == ["seg", "arc", "seg"]
    # Control: same poison without the mask stays sharp.
    out = fit_branch_arcs(run, cost, None, 100, 1,
                          max_radius_cells=10.0, min_leg_cells=3.0)
    assert [p[0] for p in out] == ["seg", "seg"]


@pytest.mark.unit
def test_emitted_fillets_become_obstacles():
    """A routed arc stamps BLOCKED cells so the next net must avoid it."""
    from dataclasses import replace

    import numpy as np

    from kicad_autorouter.board_model import ArcTrack, BoardModel, Point
    from kicad_autorouter.routing.grid import RoutingGrid, board_to_grid
    from kicad_autorouter.routing.obstacles import BLOCKED, FREE, build_occupancy_grid
    arc = ArcTrack(start=Point(0.0, 0.0), mid=Point(5.0, 9.0), end=Point(10.0, 4.0),
                   width_mm=0.25, layer="F.Cu", net_name="A")
    model = BoardModel(footprints=(), keepout_zones=(), nets={}, by_ref={},
                       arcs=(arc,))
    grid = RoutingGrid(resolution_mm=1.0, layers=("F.Cu",),
                       origin_mm=(-50.0, -50.0), width_cells=100, height_cells=100)
    cost = build_occupancy_grid(model, grid, default_clearance_mm=0.2)
    assert cost.shape == (1, 100, 100)
    c, r = board_to_grid(grid, 5.0, 9.0)
    assert cost[0, r, c] >= BLOCKED  # apex cell stamped
    c, r = board_to_grid(grid, 40.0, 40.0)
    assert cost[0, r, c] == FREE  # far interior untouched


@pytest.mark.unit
def test_fitter_tags_single_layer_callers_must_split():
    """fit_branch_arcs stamps every piece with run[0]'s layer.

    A run containing a layer change would get a cross-layer arc, so the
    pipeline must split runs at vias first (see
    test_pipeline_splits_runs_at_vias for the seam doing it).
    """
    run = [(5, 5, 0), (15, 5, 0), (15, 5, 1), (15, 15, 1)]
    out = fit_branch_arcs(run, _clear(), None, 100, 1,
                          max_radius_cells=10.0, min_leg_cells=3.0)
    layers = {p[-1] for p in out}
    assert layers == {0}  # no per-piece layer awareness here


@pytest.mark.unit
def test_steiner_joint_stays_sharp_multibranch():
    """A point shared by two branches is pinned: no arc consumes it."""
    from kicad_autorouter.routing.grid import RoutingGrid
    from kicad_autorouter.routing.pipeline import _smooth_result
    from kicad_autorouter.routing.router import RouteResult
    grid = RoutingGrid(resolution_mm=0.1, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=40, height_cells=40)
    cost = np.zeros((2, 40, 40), dtype=np.int16)
    cost[0, 10, 10] = 100  # block the (5,5)->(15,15) diagonal: the L survives
    b1 = ((5, 5, 0), (15, 5, 0), (15, 15, 0))
    b2 = ((15, 5, 0), (25, 5, 0))  # attaches at b1's interior corner
    res = RouteResult(path=list(b1) + list(b2), wire_mm=0.0, via_count=0,
                      cost=0.0, success=True, branches=(b1, b2))
    out = _smooth_result(res, cost, grid, 2,
                         fit_arcs=True, max_radius_mm=1.0, min_leg_mm=0.3)
    # No arc anywhere: the shared joint is pinned, the spoke is straight.
    assert out.arc_pieces == ()
    # And the shared joint survives as a sharp waypoint in branch 0.
    assert (15, 5, 0) in out.branches[0]


@pytest.mark.unit
def test_flag_off_is_byte_identical():
    """fit_arcs=False matches a direct smooth_grid_path call exactly."""
    from kicad_autorouter.routing.grid import RoutingGrid
    from kicad_autorouter.routing.pipeline import _smooth_result
    from kicad_autorouter.routing.router import RouteResult
    from kicad_autorouter.routing.smooth import smooth_grid_path, smoothed_wire_mm
    grid = RoutingGrid(resolution_mm=0.1, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=40, height_cells=40)
    cost = np.zeros((2, 40, 40), dtype=np.int16)
    branch = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    res = RouteResult(path=list(branch), wire_mm=0.0, via_count=0,
                      cost=0.0, success=True, branches=(tuple(branch),))
    out = _smooth_result(res, cost, grid, 2, fit_arcs=False)
    pinned = {(5, 5), (15, 15)}
    ref = smooth_grid_path(branch, cost, blocked_threshold=100,
                           corridor_radius=1, pinned=pinned, hard_radius=1)
    assert list(out.branches[0]) == ref
    assert out.wire_mm == pytest.approx(smoothed_wire_mm(ref, 0.1))
    assert out.arc_pieces == ()


@pytest.mark.unit
def test_partial_arcs_carry_server_payload_shape():
    """Failed-net partial copper with fillets serializes like the server does."""
    branch = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    pieces = fit_branch_arcs(branch, _clear(), None, 100, 1,
                             max_radius_cells=10.0, min_leg_cells=3.0)
    assert any(p[0] == "arc" for p in pieces)
    from kicad_autorouter.routing.output import routes_to_tracks_vias
    from kicad_autorouter.routing.router import RouteResult
    from kicad_autorouter.routing.grid import RoutingGrid
    grid = RoutingGrid(resolution_mm=0.1, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=40, height_cells=40)
    res = RouteResult(path=list(branch), wire_mm=0.0, via_count=0,
                      cost=0.0, success=True, branches=(tuple(branch),),
                      arc_pieces=(tuple(pieces),))
    _, _, partial_arcs = routes_to_tracks_vias({"S": res}, grid)
    assert len(partial_arcs) == 1
    a = partial_arcs[0]
    # Keys the server payloads (server.py + server_jobs.py) read.
    payload = {"start": list(a.start), "mid": list(a.mid), "end": list(a.end),
               "width_mm": a.width_mm, "layer": a.layer, "net_name": a.net_name}
    assert payload["net_name"] == "S" and payload["layer"] == "F.Cu"
    assert all(len(payload[k]) == 2 for k in ("start", "mid", "end"))


@pytest.mark.dccf
def test_dccf_single_net_fillet_no_crash():
    """One DCCF signal net with fillets on: routes, never raises."""
    from pathlib import Path
    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
    from kicad_autorouter.sexpr import parse_file
    tree = parse_file(Path("tests/fixtures/DCCF.sved.kicad_pcb"))
    model = board_model(tree)
    params = RoutingParams(run_drc=False, arc_fillets=True)
    result, _, _ = route_nets(model, ["B1"], set(), params=params,
                              original_pcb_tree=tree)
    assert result.nets_routed == 1, f"B1 must route: {result.failed_reasons}"


def test_keep_rule_pipeline_broad_arc_on_tube111():
    """Pipeline integration: C2TubeOut fillets to a multi-mm arc.

    The diagonal-hugging path already leaves the roomy corner, so the
    keep knob changes neither search cost nor arc count on this route;
    that it preserves open corners where shortcutting would erase them
    is covered by the smooth unit tests. Here we check the pipeline
    wiring end to end: arc emitted, broad radius, wire parity.
    """
    from pathlib import Path
    from kicad_autorouter.board_model import board_model, tracks
    from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
    from kicad_autorouter.sexpr import parse_file
    model = board_model(parse_file(Path("tests/fixtures/tube111.kicad_pcb")))
    tree = parse_file(Path("tests/fixtures/minimal.kicad_pcb"))

    def _route(keep, cap):
        params = RoutingParams(run_drc=False, grid_resolution_mm=0.5,
                               arc_fillets=True,
                               arc_keep_min_radius_mm=keep,
                               arc_max_radius_mm=cap)
        result, _, _ = route_nets(model, ["C2TubeOut"], set(), params=params,
                                  original_pcb_tree=tree)
        assert result.nets_routed == 1, f"C2TubeOut must route: {result.failed_reasons}"
        return result

    legacy = _route(0.0, 3.0)
    assert legacy.expansions > 0
    kept = _route(1.0, 3.0)
    assert kept.expansions == legacy.expansions  # search untouched
    assert kept.arcs_added >= 1
    assert abs(kept.total_wire_mm - legacy.total_wire_mm) / legacy.total_wire_mm <= 0.05
    # Radii from the emitted (arc) nodes: at least one broad sweep.
    radii = []
    for node in kept.pcb_tree.children("arc"):
        pts = {}
        for a in node.args:
            if a.head in ("start", "mid", "end"):
                pts[a.head] = (float(a.args[0]), float(a.args[1]))
        if len(pts) == 3:
            (ax, ay), (bx, by), (cx, cy) = pts["start"], pts["mid"], pts["end"]
            d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
            if abs(d) > 1e-12:
                ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
                      + (cx * cx + cy * cy) * (ay - by)) / d
                uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
                      + (cx * cx + cy * cy) * (bx - ax)) / d
                import math
                radii.append(math.hypot(ax - ux, ay - uy))
    assert max(radii) >= 3.0, f"expected a broad sweep, got {radii}"
    # Kept corner: no new sharp 90s vs legacy.
    def _sharp90(result):
        segs = [(t.start, t.end) for t in tracks(result.pcb_tree)
                if t.net_name == "C2TubeOut"]
        import math
        n = 0
        for (a, b), (c, d) in zip(segs, segs[1:]):
            v1 = (b.x_mm - a.x_mm, b.y_mm - a.y_mm)
            v2 = (d.x_mm - c.x_mm, d.y_mm - c.y_mm)
            n1, n2 = math.hypot(*v1) or 1e-9, math.hypot(*v2) or 1e-9
            ang = math.degrees(math.acos(max(-1.0, min(1.0,
                (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))))
            n += ang >= 80
        return n
    assert _sharp90(kept) <= _sharp90(legacy)


# ---------------------------------------------------------------------------
# Broad sweeps: one arc across a noisy region (fit_branch_arcs_with_sweeps).
# ---------------------------------------------------------------------------

def _faceted_quarter_arc(n=25, radius=20.0, origin=5, size=60):
    """Dense faceted 90-degree bend: every joint too short to fillet."""
    pts = []
    for k in range(n):
        t = math.pi * 0.5 * k / (n - 1)
        pts.append((int(round(origin + radius * math.cos(t))),
                    int(round(origin + radius * math.sin(t))), 0))
    run = [pts[0]]
    for p in pts[1:]:
        if (p[0], p[1]) != (run[-1][0], run[-1][1]):
            run.append(p)
    return run, np.zeros((1, size, size), dtype=np.int16)


def _spans_full(pieces, run):
    """True when one arc piece spans the whole run end to end."""
    ax, ay = float(run[0][0]), float(run[0][1])
    bx, by = float(run[-1][0]), float(run[-1][1])
    return any(
        p[0] == "arc"
        and math.hypot(p[1] - ax, p[2] - ay) < 1e-9
        and math.hypot(p[5] - bx, p[6] - by) < 1e-9
        for p in pieces
    )


@pytest.mark.unit
def test_broad_sweep_collapses_faceted_arc():
    """Dense cell-scale joints the tangent fitter leaves sharp become one arc."""
    run, grid = _faceted_quarter_arc()
    legacy = fit_branch_arcs(run, grid[0], None, 100, 1,
                             max_radius_cells=10.0, min_leg_cells=3.0)
    assert not any(p[0] == "arc" for p in legacy)  # short legs: all sharp
    swept = fit_branch_arcs_with_sweeps(run, grid[0], None, 100, 1,
                                        max_radius_cells=10.0, min_leg_cells=3.0)
    assert _spans_full(swept, run)
    assert len(swept) < len(legacy)


@pytest.mark.unit
def test_broad_sweep_never_spans_pinned():
    """A pinned joint inside the noise survives: no sweep erases it."""
    run, grid = _faceted_quarter_arc()
    mid = run[len(run) // 2]
    pinned = {(mid[0], mid[1])}
    swept = fit_branch_arcs_with_sweeps(run, grid[0], None, 100, 1,
                                        pinned=pinned,
                                        max_radius_cells=10.0, min_leg_cells=3.0)
    assert not _spans_full(swept, run)
    flat = [c for p in swept for c in
            ([(p[1], p[2]), (p[5], p[6])] if p[0] == "arc"
             else [(p[1], p[2]), (p[3], p[4])])]
    assert any(math.hypot(c[0] - mid[0], c[1] - mid[1]) < 1e-9 for c in flat)


@pytest.mark.unit
def test_broad_sweep_vetoed_by_blockage():
    """A BLOCKED cell on the sweep band kills the full-span arc."""
    run, grid = _faceted_quarter_arc()
    mid = run[len(run) // 2]
    grid[0, mid[1], mid[0]] = 100  # apex of the sweep band
    swept = fit_branch_arcs_with_sweeps(run, grid[0], None, 100, 1,
                                        max_radius_cells=10.0, min_leg_cells=3.0)
    assert not _spans_full(swept, run)


@pytest.mark.unit
def test_sweep_matches_legacy_on_clean_geometry():
    """Sparse chains that already fillet well come out byte-identical."""
    grid = _clear()
    runs = [
        [(5, 5, 0), (15, 5, 0), (15, 15, 0)],
        [(5, 5, 0), (15, 5, 0), (15, 15, 0), (25, 15, 0), (25, 25, 0)],
        [(5, 5, 0), (6, 5, 0), (6, 6, 0)],
    ]
    for run in runs:
        kwargs = dict(max_radius_cells=10.0, min_leg_cells=3.0)
        assert (
            fit_branch_arcs_with_sweeps(run, grid, None, 100, 1, **kwargs)
            == fit_branch_arcs(run, grid, None, 100, 1, **kwargs)
        )
    pinned = {(15, 5)}
    run = [(5, 5, 0), (15, 5, 0), (15, 15, 0)]
    assert (
        fit_branch_arcs_with_sweeps(run, grid, None, 100, 1, pinned=pinned,
                                    max_radius_cells=10.0, min_leg_cells=3.0)
        == fit_branch_arcs(run, grid, None, 100, 1, pinned=pinned,
                           max_radius_cells=10.0, min_leg_cells=3.0)
    )


# ---------------------------------------------------------------------------
# Non-90-degree tangent regression: the emission-radius fix.
# Every fillet test above sits at exactly 90 degrees, where the inverted
# radius formula coincides with the true tangent one — so a kink on every
# other angle shipped unnoticed. These tests assert G1 continuity (arc
# meets stub with matching direction) across a battery of turn angles and
# leg asymmetries, on both fitters. Sharp joints (vetoed fillets) stay
# legal; only arc joints must be tangent.
# ---------------------------------------------------------------------------

def _circumcenter(s, m, e):
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    q1, q2, q3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    ux = (q1 * (y2 - y3) + q2 * (y3 - y1) + q3 * (y1 - y2)) / d
    uy = (q1 * (x3 - x2) + q2 * (x1 - x3) + q3 * (x2 - x1)) / d
    return ux, uy


def _arc_tangent_at(s, m, e, at_start):
    """Unit tangent of the circumcircle through (s, m, e) at an endpoint,
    oriented along travel s -> e."""
    c = _circumcenter(s, m, e)
    assert c is not None, "arc points must not be collinear"
    p = s if at_start else e
    t = (-(p[1] - c[1]), p[0] - c[0])
    n = math.hypot(*t)
    assert n > 1e-12
    t = (t[0] / n, t[1] / n)
    fwd = (e[0] - s[0], e[1] - s[1])
    if t[0] * fwd[0] + t[1] * fwd[1] < 0:
        t = (-t[0], -t[1])
    return t


def _dir(a, b):
    d = (b[0] - a[0], b[1] - a[1])
    n = math.hypot(*d)
    assert n > 1e-9, f"degenerate piece leg: {a} -> {b}"
    return d[0] / n, d[1] / n


def _angle_between(u, v):
    return math.degrees(math.acos(max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))))


def assert_g1_pieces(pieces, tol_deg=0.5):
    """Every arc joint in fitted pieces must be direction-continuous.

    seg->arc: stub direction vs arc start tangent; arc->seg: arc end
    tangent vs stub direction; arc->arc: tangent vs tangent. seg->seg
    joints are sharp corners (fillet vetoed) and are always legal.
    """
    pts = []
    for p in pieces:
        if p[0] == "seg":
            pts.append(("seg", (p[1], p[2]), (p[3], p[4])))
        elif p[0] == "arc":
            pts.append(("arc", (p[1], p[2]), (p[3], p[4]), (p[5], p[6])))
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        if a[0] == "seg" and b[0] == "seg":
            continue  # sharp joint: legal
        if a[0] == "seg" and b[0] == "arc":
            joint_err = _angle_between(_dir(a[1], a[2]),
                                       _arc_tangent_at(b[1], b[2], b[3], True))
        elif a[0] == "arc" and b[0] == "seg":
            joint_err = _angle_between(_arc_tangent_at(a[1], a[2], a[3], False),
                                       _dir(b[1], b[2]))
        else:  # arc -> arc (touching fillets)
            joint_err = _angle_between(_arc_tangent_at(a[1], a[2], a[3], False),
                                       _arc_tangent_at(b[1], b[2], b[3], True))
        assert joint_err <= tol_deg, \
            f"kink {joint_err:.2f}deg between {a} and {b}"


def _corner_run(turn_deg, d1=12.0, d2=8.0):
    """Single corner: incoming along +x, outgoing rotated by turn_deg.

    Offset to y=5: the clearance gate vetos arcs whose footprint leaves
    the grid, so corners must not hug row 0.
    """
    rad = math.radians(turn_deg)
    p2 = (round(12.0 + d2 * math.cos(rad)), round(5.0 + d2 * math.sin(rad)))
    return [(0, 5, 0), (12, 5, 0), (p2[0], p2[1], 0)]


@pytest.mark.unit
@pytest.mark.parametrize("turn", [25, 30, 45, 60, 71.6, 90, 120, 135])
def test_fillet_g1_across_turn_angles(turn):
    """Fillets meet their stubs tangentially at any turn angle."""
    run = _corner_run(turn)
    for fit in (fit_branch_arcs, fit_branch_arcs_with_sweeps):
        pieces = fit(run, _clear(60), None, 100, 1,
                     max_radius_cells=10.0, min_leg_cells=0.6)
        assert any(p[0] == "arc" for p in pieces), f"turn {turn}: no fillet"
        assert_g1_pieces(pieces)


@pytest.mark.unit
@pytest.mark.parametrize("turn", [45, 71.6, 120])
def test_fillet_g1_asymmetric_legs(turn):
    """Long leg + nub leg (DCCF bend shape): tangent, no overlap."""
    run = _corner_run(turn, d1=11.0, d2=3.16)
    pieces = fit_branch_arcs_with_sweeps(run, _clear(60), None, 100, 1,
                                         max_radius_cells=20.0,
                                         min_leg_cells=0.6)
    assert any(p[0] == "arc" for p in pieces), f"turn {turn}: no fillet"
    assert_g1_pieces(pieces)
    sharp = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                for a, b, _ in zip(run, run[1:], range(len(run))))
    assert pieces_wire_cells(pieces) < sharp  # arc shortens the corner


@pytest.mark.unit
def test_fillet_g1_multi_joint_runs():
    """S-curves, shallow zigzags, obtuse chains: every arc joint tangent."""
    runs = [
        [(0, 5, 0), (10, 5, 0), (15, 10, 0), (25, 10, 0)],    # S, +/-45
        [(0, 5, 0), (10, 6, 0), (20, 5, 0), (30, 6, 0)],      # shallow zigzag
        [(0, 5, 0), (12, 5, 0), (18, 15, 0), (14, 25, 0)],    # obtuse chain
        [(0, 5, 0), (11, 5, 0), (12, 8, 0)],                  # DCCF nub shape
        [(5, 5, 0), (15, 5, 0), (15, 15, 0), (25, 15, 0)],    # elbow pair
    ]
    for run in runs:
        for fit in (fit_branch_arcs, fit_branch_arcs_with_sweeps):
            pieces = fit(run, _clear(60), None, 100, 1,
                         max_radius_cells=20.0, min_leg_cells=0.6)
            assert_g1_pieces(pieces)
