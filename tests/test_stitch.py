"""Automatic GND continuity repair: stitching vias + bridge tracks (Phase 2).

- Overlap case: severed F.Cu pour over intact B.Cu pour → one via joining
  the groups, placed on copper on both layers with annulus clearance.
- Bridge case: single-layer sever with a short detour → GND tracks that
  stay clear of foreign copper by width/2 + clearance.
- Nothing to do: unfragmented pour → empty plan.
- Region isolation: panels sharing one file never join across regions.
- Budgets + determinism pins.
"""

import numpy as np
import pytest

from kicad_autorouter.board_model import BoardRegion, Point
from kicad_autorouter.routing.grid import board_to_grid, create_grid_from_model
from kicad_autorouter.routing.pour import pour_islands
from kicad_autorouter.routing.stitch import _foreign_mask, plan_stitches

from .test_pour import _board, _rect_zone, _track
from .helpers import make_fp


def _two_layer_severed():
    fp = make_fp("J1", "uuid-j1", 5.0, 15.0, ("GND",), tht=True)
    return _board(
        fps=[fp],
        zones=[_rect_zone("z1", "GND", ("F.Cu",)),
               _rect_zone("z2", "GND", ("B.Cu",))],
        tracks=[_track(20.0, -5.0, 20.0, 35.0, "SIG")],
    )


def _single_layer_severed():
    fp = make_fp("J1", "uuid-j1", 5.0, 6.0, ("GND",), tht=True)
    return _board(
        fps=[fp],
        zones=[_rect_zone("z1", "GND", ("F.Cu",), y1=12.0)],
        tracks=[_track(20.0, -5.0, 20.0, 17.0, "SIG")],
    )


@pytest.mark.unit
class TestStitchVias:
    def test_overlap_sever_gets_one_via(self):
        model = _two_layer_severed()
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid)
        assert plan.islands_before == 3
        assert len(plan.vias) == 1
        assert plan.islands_after == 1
        via = plan.vias[0]
        assert via.net_name == "GND"
        assert via.layers == ("F.Cu", "B.Cu")
        assert via.size_mm == pytest.approx(0.8)
        assert via.drill_mm == pytest.approx(0.4)

    def test_via_sits_on_copper_both_layers(self):
        from kicad_autorouter.routing.pour import pour_copper_mask
        model = _two_layer_severed()
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid)
        via = plan.vias[0]
        c, r = board_to_grid(grid, via.position.x_mm, via.position.y_mm)
        for layer in ("F.Cu", "B.Cu"):
            copper = pour_copper_mask(model, grid, "GND", layer)
            assert copper[r, c], f"via not on {layer} copper"

    def test_via_annulus_clear_of_foreign(self):
        model = _two_layer_severed()
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid)
        via = plan.vias[0]
        # Annulus (size/2 + clearance) must hold no foreign copper/drills.
        for layer in ("F.Cu", "B.Cu"):
            foreign = _foreign_mask(model, grid, "GND", layer, 0.6)
            c, r = board_to_grid(grid, via.position.x_mm, via.position.y_mm)
            H, W = foreign.shape
            rad = 2  # spot-check the via cell + ring
            window = foreign[max(0, r - rad):r + rad + 1, max(0, c - rad):c + rad + 1]
            assert not window.any(), f"foreign copper in via annulus on {layer}"

    def test_unfragmented_board_empty_plan(self):
        model = _board(zones=[_rect_zone("z1", "GND", ("F.Cu",))])
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid)
        assert plan.vias == [] and plan.tracks == []
        assert plan.islands_before == 1
        assert plan.islands_after == 1

    def test_stacked_pours_get_one_stitch(self):
        # Same outline on both layers, no barrels: F and B pours are
        # genuinely disconnected → exactly one stitch via joins them.
        model = _board(zones=[_rect_zone("z1", "GND", ("F.Cu", "B.Cu"))])
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid)
        assert plan.islands_before == 2
        assert len(plan.vias) == 1
        assert plan.islands_after == 1

    def test_max_vias_zero_disables(self):
        model = _two_layer_severed()
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid, max_vias=0, max_bridges=0)
        assert plan.vias == [] and plan.tracks == []

    def test_deterministic(self):
        model = _two_layer_severed()
        grid = create_grid_from_model(model)
        a = plan_stitches(model, grid)
        b = plan_stitches(model, grid)
        assert [(v.position.x_mm, v.position.y_mm) for v in a.vias] == \
               [(v.position.x_mm, v.position.y_mm) for v in b.vias]


@pytest.mark.unit
class TestBridgeTracks:
    def test_single_layer_sever_gets_bridge(self):
        model = _single_layer_severed()
        grid = create_grid_from_model(model)
        assert len(pour_islands(model, grid, "GND")["F.Cu"]) == 2
        plan = plan_stitches(model, grid)
        assert plan.vias == []  # no opposite-layer copper to stitch to
        assert len(plan.tracks) > 0
        assert plan.islands_after == 1
        for track in plan.tracks:
            assert track.net_name == "GND"
            assert track.layer == "F.Cu"
            assert track.width_mm == pytest.approx(0.5)

    def test_bridge_clears_foreign_copper(self):
        from kicad_autorouter.routing.grid import grid_to_board
        model = _single_layer_severed()
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid, bridge_width_mm=0.5, clearance_mm=0.2)
        # Every bridge cell must clear foreign copper by width/2 + clearance.
        foreign = _foreign_mask(model, grid, "GND", "F.Cu", 0.0)
        res = grid.resolution_mm
        need = int(0.5 / 2 / res + 0.2 / res) + 1
        H, W = foreign.shape
        for track in plan.tracks:
            for end in (track.start, track.end):
                c, r = board_to_grid(grid, end.x_mm, end.y_mm)
                window = foreign[max(0, r - need):r + need + 1,
                                 max(0, c - need):c + need + 1]
                assert not window.any(), f"bridge endpoint too close to foreign @ {end}"

    def test_max_bridges_zero_skips_tracks(self):
        model = _single_layer_severed()
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid, max_bridges=0)
        assert plan.tracks == []
        assert plan.islands_after == 2  # joined network + unjoined group


@pytest.mark.unit
class TestRegionIsolation:
    def _two_panels(self):
        # Two disjoint 40x30 panels; panel A severed, panel B intact.
        ra = BoardRegion(
            uuid="ra", polygon=(Point(0, 0), Point(40, 0), Point(40, 30), Point(0, 30)),
            bbox=(0.0, 40.0, 0.0, 30.0),
        )
        rb = BoardRegion(
            uuid="rb",
            polygon=(Point(60, 0), Point(100, 0), Point(100, 30), Point(60, 30)),
            bbox=(60.0, 100.0, 0.0, 30.0),
        )
        za = _rect_zone("za", "GND", ("F.Cu", "B.Cu"))
        zb = _rect_zone(
            "zb", "GND", ("F.Cu", "B.Cu"),
            x0=60.0, y0=0.0, x1=100.0, y1=30.0,
        )
        fa = make_fp("JA", "uuid-ja", 5.0, 15.0, ("GND",), tht=True)
        fb = make_fp("JB", "uuid-jb", 65.0, 15.0, ("GND",), tht=True)
        model = _board(
            fps=[fa, fb], zones=[za, zb],
            tracks=[_track(20.0, -5.0, 20.0, 35.0, "SIG")],
        )
        from dataclasses import replace
        return replace(
            model,
            board_regions=(ra, rb),
            footprint_region={"uuid-ja": 0, "uuid-jb": 1},
        )

    def test_no_cross_region_join(self):
        model = self._two_panels()
        grid = create_grid_from_model(model)
        plan = plan_stitches(model, grid)
        # Panel A repaired internally; panel B untouched; regions never merge.
        for via in plan.vias:
            assert via.position.x_mm < 40.0, f"via leaked across panels: {via.position}"
        for track in plan.tracks:
            for end in (track.start, track.end):
                assert end.x_mm < 40.0, f"bridge leaked across panels: {end}"
        assert plan.islands_after == 2  # one network per panel


@pytest.mark.unit
class TestWriteback:
    """Stitch vias survive writeback and re-parse with their GND net."""

    def test_stitch_vias_in_tree_and_reparsed(self):
        from pathlib import Path

        from kicad_autorouter.board_model import board_model
        from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
        from kicad_autorouter.sexpr import parse_file

        tree = parse_file(Path(__file__).parent / "fixtures" / "DCCF.sved.kicad_pcb")
        model = board_model(tree)
        small = [n for n, conns in model.nets.items() if 2 <= len(conns) <= 6]
        net = sorted(small)[0]
        res, _, _ = route_nets(
            model, [net], set(),
            params=RoutingParams(run_drc=False),
            original_pcb_tree=tree,
        )
        assert res.nets_routed == 1
        assert res.stitch_vias_added + res.stitch_tracks_added >= 1
        assert res.pcb_tree is not None
        gnd_copper = [
            v for v in res.pcb_tree.args
            if getattr(v, "head", None) in ("via", "segment")
            and v.find("net") is not None
            and v.find("net").args and v.find("net").args[-1] == "GND"
        ]
        assert len(gnd_copper) >= 1
        reparsed = board_model(res.pcb_tree)
        assert any(v.net_name == "GND" for v in reparsed.vias) or any(
            t.net_name == "GND" for t in reparsed.tracks
        )
