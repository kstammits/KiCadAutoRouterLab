"""Tests for cost grid building primitives (obstacles)."""

import pytest
import numpy as np
from shapely.geometry import Polygon

from kicad_autorouter.board_model import BoardModel, Footprint, Pad, Point
from kicad_autorouter.routing.grid import create_grid_from_model, board_to_grid, grid_to_board
from kicad_autorouter.routing.obstacles import (
    build_occupancy_grid,
    build_fanout_cost,
    build_via_block_mask,
    FREE, COURTYARD, BLOCKED, HIGH_COST, EDGE_KEEPOUT,
    FANOUT_HALO_INNER, FANOUT_HALO_OUTER, THT_EXCLUSION,
    _block_courtyard,
    _mark_pad,
    _mark_zone,
    _mark_edge_keepout,
)

from .helpers import make_fp as make_test_fp


class TestCourtyardBlocking:
    """Tests for _block_courtyard."""

    def test_block_courtyard_marks_both_layers(self, grid, minimal_model):
        """Courtyard outlines mark F.Cu and B.Cu as COURTYARD (not BLOCKED).

        Courtyards are placement guides, not copper keepouts: tracks route
        through them freely. COURTYARD (25) is below SOFT_THRESHOLD (50) so
        the smoother can shortcut through courtyard outlines.
        """
        fp = next(fp for fp in minimal_model.footprints if fp.ref == "MH1")
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)

        _block_courtyard(cost_grid, grid, fp)

        # Both layers should have COURTYARD cells, none BLOCKED
        assert np.any(cost_grid[0] == COURTYARD)
        assert np.any(cost_grid[1] == COURTYARD)
        assert not np.any(cost_grid >= BLOCKED)


class TestFanoutCost:
    """Tests for build_fanout_cost (per-net secondary halo grid)."""

    def _two_pad_model(self, grid):
        from dataclasses import replace

        from kicad_autorouter.board_model import BoardModel
        x0, y0 = grid.origin_mm[0] + 50, grid.origin_mm[1] + 50

        def _pad_only_fp(ref, uuid, x, net):
            fp = make_test_fp(ref, uuid, x, y0, (net,),
                              footprint_id="",
                              pad_shape="circle", pad_layers=("F.Cu",))
            return replace(fp, courtyard=())

        return BoardModel(
            footprints=(
                _pad_only_fp("R1", "uuid-r1", x0, "A"),
                _pad_only_fp("R2", "uuid-r2", x0 + 10, "B"),
            ),
            keepout_zones=(),
            nets={},
            by_ref={},
        ), x0, y0

    def test_own_net_excluded_others_penalized(self, grid):
        """Current net's pads stay 0; other nets get hard exclusion + outer halo."""
        model, x0, y0 = self._two_pad_model(grid)
        secondary = build_fanout_cost(model, grid, "A",
                                      clearance_mm=0.2, track_half_mm=0.125)
        assert secondary.shape == (2, grid.height_cells, grid.width_cells)
        assert secondary.dtype.name == "int16"
        # Own pad (R1, net A): no halo
        c1, r1 = board_to_grid(grid, x0, y0)
        assert secondary[0, r1, c1] == 0
        # Other pad (R2, net B): hard exclusion at center (SMD pads now get THT_EXCLUSION)
        c2, r2 = board_to_grid(grid, x0 + 10, y0)
        assert secondary[0, r2, c2] == THT_EXCLUSION
        # SMD pad on F.Cu only: B.Cu untouched
        assert secondary[1, r2, c2] == 0
        # Outer ring present beyond inner (soft shoulder, strictly weaker)
        assert secondary[0].max() == THT_EXCLUSION
        assert FANOUT_HALO_OUTER in secondary[0]


class TestNoNetPadHalos:
    """Tests for no-net pad handling in fanout cost."""

    def _no_net_pad_model(self, grid, pad_type="np_thru_hole", pad_layers=("F.Cu", "B.Cu")):
        """Create model with a no-net mounting hole pad."""

        x0, y0 = grid.origin_mm[0] + 50, grid.origin_mm[1] + 50

        # Create a pad with net_name=None (mounting hole style)
        pad = Pad(
            number="MP",
            net_name=None,
            position=Point(x_mm=x0, y_mm=y0),
            size_mm=(2.0, 2.0),
            shape="circle",
            angle_deg=0.0,
            pad_type=pad_type,
            drill_mm=1.6 if pad_type in ("thru_hole", "np_thru_hole") else 0.0,
            layers=pad_layers,
        )

        fp = Footprint(
            ref="MH1",
            footprint_id="MountingHole",
            layer="F.Cu",
            x_mm=x0,
            y_mm=y0,
            angle_deg=0.0,
            pads=(pad,),
            courtyard=(),
            locked=False,
            uuid="uuid-mh1",
            ghost=False,
            ghost_attractions=(),
        )

        return BoardModel(
            footprints=(fp,),
            keepout_zones=(),
            nets={},
            by_ref={"MH1": fp},
        ), x0, y0

    def test_no_net_tht_pad_gets_halo_both_layers(self, grid):
        """No-net THT pad gets outer halo + THT_EXCLUSION on both layers."""
        model, x0, y0 = self._no_net_pad_model(grid, "np_thru_hole", ("F.Cu", "B.Cu"))
        secondary = build_fanout_cost(model, grid, "SOME_NET",
                                      clearance_mm=0.2, track_half_mm=0.125)

        c, r = board_to_grid(grid, x0, y0)

        # Both layers should have outer halo
        assert secondary[0, r, c] >= FANOUT_HALO_OUTER
        assert secondary[1, r, c] >= FANOUT_HALO_OUTER

        # Both layers should have THT_EXCLUSION (hard ring)
        assert secondary[0, r, c] == THT_EXCLUSION
        assert secondary[1, r, c] == THT_EXCLUSION

    def test_no_net_smd_pad_gets_halo_single_layer(self, grid):
        """No-net SMD pad gets hard exclusion + outer halo on its layer only."""
        model, x0, y0 = self._no_net_pad_model(grid, "smd", ("F.Cu",))
        secondary = build_fanout_cost(model, grid, "SOME_NET",
                                      clearance_mm=0.2, track_half_mm=0.125)

        c, r = board_to_grid(grid, x0, y0)

        # F.Cu should have hard exclusion (THT_EXCLUSION) at center
        assert secondary[0, r, c] == THT_EXCLUSION
        # Outer ring should exist nearby
        assert np.any(secondary[0] == FANOUT_HALO_OUTER)

        # B.Cu should be untouched (SMD on F.Cu only)
        assert secondary[1, r, c] == 0

    def test_unconnected_net_pad_gets_normal_halo(self, grid):
        """Pads with 'unconnected-' net name get normal halos (not skipped)."""
        x0, y0 = grid.origin_mm[0] + 50, grid.origin_mm[1] + 50

        pad = Pad(
            number="2",
            net_name="unconnected-(Q2-Pad2)",
            position=Point(x_mm=x0, y_mm=y0),
            size_mm=(1.0, 1.0),
            shape="rect",
            angle_deg=0.0,
            pad_type="smd",
            drill_mm=0.0,
            layers=("F.Cu", "F.Paste", "F.Mask"),
        )

        fp = Footprint(
            ref="Q2",
            footprint_id="TestConn",
            layer="F.Cu",
            x_mm=x0,
            y_mm=y0,
            angle_deg=0.0,
            pads=(pad,),
            courtyard=(),
            locked=False,
            uuid="uuid-q2",
            ghost=False,
            ghost_attractions=(),
        )

        model = BoardModel(
            footprints=(fp,),
            keepout_zones=(),
            nets={},
            by_ref={"Q2": fp},
        )

        secondary = build_fanout_cost(model, grid, "OTHER_NET",
                                      clearance_mm=0.2, track_half_mm=0.125)

        c, r = board_to_grid(grid, x0, y0)

        # Should get hard exclusion (not skipped, not own net)
        assert secondary[0, r, c] == THT_EXCLUSION
        # Outer ring should exist nearby
        assert np.any(secondary[0] == FANOUT_HALO_OUTER)
        # B.Cu untouched (SMD)
        assert secondary[1, r, c] == 0


class TestPadMarking:
    """Tests for _mark_pad."""

    def test_mark_pad_tht_both_layers(self, grid, minimal_model):
        """Through-hole pad marks both layers as HIGH_COST."""
        # Find a THT pad
        tht_fp = None
        for fp in minimal_model.footprints:
            for pad in fp.pads:
                if pad.is_through_hole:
                    tht_fp = fp
                    break
            if tht_fp:
                break

        if not tht_fp:
            pytest.skip("No THT pads in minimal fixture")

        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        tht_pad = next(p for p in tht_fp.pads if p.is_through_hole)

        _mark_pad(cost_grid, grid, tht_fp, tht_pad, {}, 2)

        col, row = board_to_grid(grid, tht_pad.position.x_mm, tht_pad.position.y_mm)
        # Should mark HIGH_COST on both layers around pad
        assert cost_grid[0, row, col] >= HIGH_COST
        assert cost_grid[1, row, col] >= HIGH_COST

    def test_mark_pad_smd_single_layer(self, grid, minimal_model):
        """SMD pad marks only its layer."""
        smd_fp = None
        smd_pad = None
        for fp in minimal_model.footprints:
            for pad in fp.pads:
                if not pad.is_through_hole:
                    smd_fp = fp
                    smd_pad = pad
                    break
            if smd_fp:
                break

        if not smd_fp:
            pytest.skip("No SMD pads in minimal fixture")

        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        _mark_pad(cost_grid, grid, smd_fp, smd_pad, {}, 2)

        col, row = board_to_grid(grid, smd_pad.position.x_mm, smd_pad.position.y_mm)
        layer_idx = 0 if smd_fp.layer == "F.Cu" else 1
        other_layer = 1 - layer_idx

        # Only pad's layer should be marked
        assert cost_grid[layer_idx, row, col] >= HIGH_COST
        assert cost_grid[other_layer, row, col] == FREE


class TestZoneMarking:
    """Tests for _mark_zone."""

    def test_zone_marking_keeps_thin_strips_thin(self, grid, minimal_model):
        """Regression: minimal's full-width keepout strips must not become
        full-height blocked bars (board_to_grid returns (col, row)).

        Columns outside the board outline are hard-blocked by design
        (outside copper is impassable), so only inside-board columns are
        checked here.
        """
        from kicad_autorouter.routing.obstacles import inside_board_mask

        cost_grid = build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)
        H, W = cost_grid.shape[1], cost_grid.shape[2]
        inside = inside_board_mask(grid, minimal_model)
        for layer_idx in range(cost_grid.shape[0]):
            blocked = cost_grid[layer_idx] >= BLOCKED
            if inside is not None:
                # Only columns with inside-board cells can fail this test;
                # margin columns outside the outline are fully blocked.
                full_height_cols = [
                    c for c in range(W)
                    if inside[:, c].any()
                    and (blocked[:, c] & inside[:, c]).sum() >= inside[:, c].sum() - 2
                ]
            else:
                full_height_cols = np.where(blocked.sum(axis=0) >= H - 2)[0]
            assert len(full_height_cols) == 0


class TestPadInsideCourtyard:
    """Regression: SMD pad enclosed by its own courtyard must escape."""

    def test_pad_inside_own_courtyard_can_route_out(self, grid):
        """Regression: an SMD pad enclosed by its own courtyard outline
        must still reach a cell outside it (outlines are HIGH_COST)."""
        from kicad_autorouter.routing.router import SingleNetRouter, CostMap

        x_mm = grid.origin_mm[0] + 50
        y_mm = grid.origin_mm[1] + 50
        fp = make_test_fp("T1", "uuid-t1", x_mm, y_mm, ("N1",),
                          footprint_id="", hw=1.5, hh=1.0,
                          pad_shape="circle", pad_layers=("F.Cu",))
        cost_grid = np.full((2, grid.height_cells, grid.width_cells), FREE, dtype=np.int16)
        _block_courtyard(cost_grid, grid, fp)

        sc, sr = board_to_grid(grid, x_mm, y_mm)
        gc, gr = board_to_grid(grid, x_mm + 10, y_mm)
        router = SingleNetRouter(grid, cost_grid, CostMap(),
                                 np.zeros((grid.height_cells, grid.width_cells), dtype=bool))
        result = router._a_star((sc, sr, 0), (gc, gr, 0))
        assert result.success