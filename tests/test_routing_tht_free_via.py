"""Free layer transitions at THT pads (R6): pre-drilled hole as free via.

Net TFREE joins a west SMD pad on F.Cu to an east SMD pad on B.Cu with a
single-pad through-hole component mid-board on the same net. Two keepout
walls (F.Cu east of the THT, B.Cu west of it) close the walk-around
shortcuts, so the cheapest route is: F.Cu into the THT, free hop (0 via
cost inside the drill disc), B.Cu out — with NO new via drilled, because
the plated barrel already exists.

A second net NFOREIGN runs straight through the THT site on F.Cu; its
copper must detour around the hard exclusion ring (hole + annulus +
track width + clearance), proving foreign tracks keep clear while the
owner connects.

Thresholds calibrated by running (0.5mm grid routes in <1s); the
properties are structural, not numeric.
"""

import math
from dataclasses import replace
from pathlib import Path

from kicad_autorouter.board_model import (
    Footprint,
    KeepoutZone,
    Point,
    board_model,
)
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.grid import RoutingGrid, board_to_grid, create_grid_from_model
from kicad_autorouter.routing.obstacles import (
    BLOCKED,
    THT_EXCLUSION,
    build_fanout_cost,
    build_occupancy_grid,
)
from kicad_autorouter.routing.output import routes_to_tracks_vias
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
from kicad_autorouter.routing.router import CostMap, SECONDARY_BLOCKED, SingleNetRouter
from kicad_autorouter.sexpr import parse_file

from .helpers import (
    closest_approach,
    make_fp,
    make_rect_courtyard,
    make_tht_pad,
    plan_segments,
)

FIXTURES = Path("tests/fixtures")
RES = 0.5  # coarse grid: fast, still resolves 0.8mm drills

# Minimal fixture outline is (55,45)-(215,145); mid-board freeway at y=95.
W_SMD = (80.0, 95.0)    # TFREE west terminal, F.Cu
E_SMD = (190.0, 95.0)   # TFREE east terminal, B.Cu
THT_POS = (135.0, 95.0)  # TFREE through-hole, same net
# (drill 0.8 / land 2.0 — see make_tht_pad defaults)

# NFOREIGN straddles the THT vertically on F.Cu; its straight shot
# passes exactly through the hole center.
F1_POS = (135.0, 70.0)
F2_POS = (135.0, 120.0)


def _smd_resistor(ref: str, uuid: str, x_mm: float, y_mm: float,
                  net_a: str, net_b: str, layer: str = "F.Cu") -> Footprint:
    lib = "Test:R_0805_THT" if layer == "F.Cu" else "Test:R_0805_THT_B"
    return make_fp(ref, uuid, x_mm, y_mm, (net_a, net_b),
                   layer=layer, footprint_id=lib)


def _tht_part(ref: str, uuid: str, x_mm: float, y_mm: float, net: str) -> Footprint:
    pads = (replace(make_tht_pad("1", net), position=Point(x_mm, y_mm)),)
    return Footprint(
        ref=ref, footprint_id="Test:PinHeader_1x01",
        layer="F.Cu", x_mm=x_mm, y_mm=y_mm, angle_deg=0.0,
        pads=pads, courtyard=make_rect_courtyard(x_mm, y_mm, hw=1.0),
        locked=False, uuid=uuid, ghost=False, ghost_attractions=(),
    )


def _wall(uuid: str, layers, x0: float, x1: float, y0: float = 45.0, y1: float = 145.0) -> KeepoutZone:
    return KeepoutZone(
        uuid=uuid, layers=tuple(layers),
        polygon=(Point(x0, y0), Point(x1, y0), Point(x1, y1), Point(x0, y1)),
        tracks_allowed=False, vias_allowed=False, pads_allowed=False,
        copper_pour_allowed=False, footprints_allowed=False,
    )


def _tfree_model(with_tht: bool = True, with_foreign: bool = False):
    """Minimal fixture + TFREE west(F)/east(B) (+ THT middle) footprints."""
    from dataclasses import replace as dc_replace
    pcb_tree = parse_file(FIXTURES / "minimal.kicad_pcb")
    pcb_tree = add_footprint_to_tree(
        pcb_tree,
        _smd_resistor("TW", "tht-free-w-uuid-0001", *W_SMD, "TFREE", "STUB_W"),
        "Test:R_0805_THT",
    )
    pcb_tree = add_footprint_to_tree(
        pcb_tree,
        _smd_resistor("TE", "tht-free-e-uuid-0002", *E_SMD, "TFREE", "STUB_E", layer="B.Cu"),
        "Test:R_0805_THT_B",
    )
    if with_tht:
        pcb_tree = add_footprint_to_tree(
            pcb_tree, _tht_part("TH", "tht-free-h-uuid-0003", *THT_POS, "TFREE"),
            "Test:PinHeader_1x01",
        )
    if with_foreign:
        pcb_tree = add_footprint_to_tree(
            pcb_tree,
            _smd_resistor("F1", "tht-free-f1-uuid-0004", *F1_POS, "NFOREIGN", "STUB_F1"),
            "Test:R_0805_THT",
        )
        pcb_tree = add_footprint_to_tree(
            pcb_tree,
            _smd_resistor("F2", "tht-free-f2-uuid-0005", *F2_POS, "NFOREIGN", "STUB_F2"),
            "Test:R_0805_THT",
        )
    model = board_model(pcb_tree)
    # Keepout walls close the walk-arounds: F.Cu bar east of the THT,
    # B.Cu bar west of it. Injected at model level (writeback only
    # carries tracks/vias, so the tree needs no zone writer).
    walls = (
        _wall("tht-free-kz-f", ("F.Cu",), 150.0, 160.0),
        _wall("tht-free-kz-b", ("B.Cu",), 110.0, 120.0),
    )
    model = dc_replace(model, keepout_zones=model.keepout_zones + walls)
    return pcb_tree, model


def test_tfree_routes_through_tht_with_no_new_via():
    pcb_tree, model = _tfree_model()
    assert {c.ref for c in model.nets["TFREE"]} == {"TW", "TE", "TH"}

    result, _, _ = route_nets(
        model, ["TFREE"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=RES),
        original_pcb_tree=pcb_tree,
    )
    assert result.nets_routed == 1, f"TFREE must route: {result}"
    assert result.nets_failed == 0
    # The hop reuses the plated barrel: no new via drilled.
    assert result.vias_added == 0, f"expected vialess THT hop, got {result.vias_added}"

    assert result.pcb_tree is not None
    f_segs = plan_segments(result.pcb_tree, "TFREE", "F.Cu")
    b_segs = plan_segments(result.pcb_tree, "TFREE", "B.Cu")
    assert f_segs, "TFREE must enter on F.Cu (west pad side)"
    assert b_segs, "TFREE must leave on B.Cu (east pad side)"
    # Both copper runs meet at the hole: each approaches within ~2
    # cells (1.0mm @0.5mm grid) of the THT center (grid snap + smoothing
    # keep exact coincidence out of reach; the vialess hop is the proof).
    assert closest_approach(f_segs, *THT_POS) <= 1.0, "F.Cu run must reach the hole"
    assert closest_approach(b_segs, *THT_POS) <= 1.0, "B.Cu run must leave from the hole"


def test_tfree_hop_is_free_not_cheap():
    """Even an absurd via price must still route through the THT hole."""
    pcb_tree, model = _tfree_model()
    result, _, _ = route_nets(
        model, ["TFREE"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=RES, via_cost_mm=1000.0),
        original_pcb_tree=pcb_tree,
    )
    assert result.nets_routed == 1, f"free hop must survive via price 1000: {result}"
    assert result.vias_added == 0


def test_no_tht_control_pays_one_via():
    """Without the THT part the same terminals cost exactly one drilled via."""
    pcb_tree, model = _tfree_model(with_tht=False)
    result, _, _ = route_nets(
        model, ["TFREE"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=RES),
        original_pcb_tree=pcb_tree,
    )
    assert result.nets_routed == 1, f"control must still route: {result}"
    assert result.vias_added == 1, f"control must drill one via, got {result.vias_added}"


def test_via_price_grid_independent():
    """Same mm price routes vialess at coarse and fine grids."""
    for res in (0.5, 0.25):
        pcb_tree, model = _tfree_model()
        result, _, _ = route_nets(
            model, ["TFREE"], set(),
            params=RoutingParams(run_drc=False, grid_resolution_mm=res),
            original_pcb_tree=pcb_tree,
        )
        assert result.nets_routed == 1, f"res {res}: {result}"
        assert result.vias_added == 0, f"res {res}: {result}"


def test_via_cost_mm_scales_to_cells():
    """Router-level: one CostMap prices the hop per grid resolution."""
    for res, expected in ((0.1, 50), (0.5, 10)):
        grid = RoutingGrid(resolution_mm=res, layers=("F.Cu", "B.Cu"),
                           origin_mm=(0.0, 0.0), width_cells=10, height_cells=10)
        import numpy as np
        router = SingleNetRouter(grid, np.zeros((2, 10, 10), dtype=np.int16), CostMap())
        assert router._via_cost_cells == expected, (res, router._via_cost_cells)
        assert router._via_cost_cells == CostMap().via_cost_cells(res)


def test_foreign_net_holds_tht_ring():
    """NFOREIGN's straight shot crosses the hole site: it must detour."""
    pcb_tree, model = _tfree_model(with_foreign=True)
    result, _, routing_grid = route_nets(
        model, ["TFREE", "NFOREIGN"], set(),
        params=RoutingParams(run_drc=False, grid_resolution_mm=RES),
        original_pcb_tree=pcb_tree,
    )
    assert result.nets_routed == 2, f"both nets must route: {result}"
    assert result.pcb_tree is not None
    f_segs = plan_segments(result.pcb_tree, "NFOREIGN", "F.Cu")
    b_segs = plan_segments(result.pcb_tree, "NFOREIGN", "B.Cu")
    all_segs = f_segs + b_segs
    assert all_segs, "NFOREIGN must emit copper"
    # Exclusion ring inner radius for the THT pad: half land (1.0) +
    # clearance (0.2) + track half (0.125) = 1.325mm; the detour must
    # hold outside it with grid tolerance to spare.
    gap = closest_approach(all_segs, *THT_POS)
    assert gap >= 1.0, f"foreign copper must hold the ring, closest {gap:.2f}mm"
    # ...while the owner's hop stays vialess (NFOREIGN may drill its own
    # legitimate dodge vias — those are checked below, not here).
    from kicad_autorouter.board_model import vias as board_vias
    tree_vias = board_vias(result.pcb_tree)
    assert not [v for v in tree_vias if v.net_name == "TFREE"], "owner hop must stay vialess"
    # NFOREIGN may hop layers to dodge the ring (legitimate); any such
    # hop must itself land outside the exclusion ring.
    for v in tree_vias:
        if v.net_name == "NFOREIGN":
            d = math.hypot(v.position.x_mm - THT_POS[0], v.position.y_mm - THT_POS[1])
            assert d >= 1.0, f"foreign hop must land outside the ring, at {d:.2f}mm"


def test_keepout_blocks_tracks_only_when_disallowed():
    """Unit: keepout walls mark BLOCKED on their layer, skipped if allowed."""
    from dataclasses import replace as dc_replace
    _, model = _tfree_model()
    grid = create_grid_from_model(model, RES, 2.0)
    cost = build_occupancy_grid(model, grid, default_clearance_mm=0.2)
    # F-wall cell (155, 95): blocked on F.Cu, free-ish on B.Cu.
    c, r = board_to_grid(grid, 155.0, 95.0)
    assert cost[0, r, c] >= BLOCKED, "F keepout must block F.Cu"
    assert cost[1, r, c] < BLOCKED, "F keepout must not touch B.Cu"
    # Allowed keepout is ignored entirely.
    open_wall = _wall("open", ("F.Cu",), 150.0, 160.0)
    open_wall = dc_replace(open_wall, tracks_allowed=True)
    model2 = dc_replace(model, keepout_zones=(open_wall,))
    cost2 = build_occupancy_grid(model2, grid, default_clearance_mm=0.2)
    assert cost2[0, r, c] < BLOCKED


def test_exclusion_ring_values():
    """Unit: foreign THT disc is blocking, own net sees zero."""
    _, model = _tfree_model()
    grid = create_grid_from_model(model, RES, 2.0)
    c, r = board_to_grid(grid, *THT_POS)
    foreign = build_fanout_cost(model, grid, "NFOREIGN", clearance_mm=0.2, track_half_mm=0.125)
    assert foreign[0, r, c] >= SECONDARY_BLOCKED
    assert foreign[1, r, c] >= SECONDARY_BLOCKED
    assert foreign[0, r, c] == THT_EXCLUSION
    own = build_fanout_cost(model, grid, "TFREE", clearance_mm=0.2, track_half_mm=0.125)
    assert own[0, r, c] == 0 and own[1, r, c] == 0


def test_suppression_only_on_own_tht_site():
    """Unit: output drops the via on own holes, keeps foreign hops."""
    from kicad_autorouter.routing.router import RouteResult
    grid = RoutingGrid(resolution_mm=RES, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=10, height_cells=10)
    hop = RouteResult(path=[(2, 2, 0), (2, 2, 1)], wire_mm=0.0, via_count=1, cost=0.0, success=True)
    _, vias = routes_to_tracks_vias({"N": hop}, grid, tht_sites={"N": {(2, 2)}})
    assert vias == [], "own-hole hop must not drill"
    _, vias = routes_to_tracks_vias({"N": hop}, grid, tht_sites={"OTHER": {(2, 2)}})
    assert len(vias) == 1, "foreign-hole hop still drills (router should avoid it)"
    _, vias = routes_to_tracks_vias({"N": hop}, grid)
    assert len(vias) == 1, "no sites: legacy behavior"


def test_secondary_blocking_never_traps_own_terminals():
    """Router-level: a fully ringed foreign THT still routes around it."""
    import numpy as np
    grid = RoutingGrid(resolution_mm=RES, layers=("F.Cu", "B.Cu"),
                       origin_mm=(0.0, 0.0), width_cells=20, height_cells=20)
    cost = np.zeros((2, 20, 20), dtype=np.int16)
    secondary = np.zeros((2, 20, 20), dtype=np.int16)
    secondary[:, 8:13, 8:13] = THT_EXCLUSION  # 5x5 foreign ring mid-board
    router = SingleNetRouter(grid, cost, CostMap())
    result = router.route([(2, 10, 0), (17, 10, 0)], secondary)
    assert result.success, "must detour around the ring, not fail"
    assert not any((c, r) in {(c0, r0) for c0 in range(8, 13) for r0 in range(8, 13)}
                   for c, r, _ in result.path), "path must not enter the ring"
