"""Raster-guard regression tests for foreign-THT hard exclusion.

Covers the tube115 J2 short: a GND track grazed a foreign THT pad corner
(~0.05 mm copper overlap) through a boundary cell whose center fell just
outside the cell-center-sampled exclusion stamp. The hard-core tier now
carries a half-diagonal raster guard; this pins that contract at the
stamp layer (deterministic, no routing needed).
"""

import math

import numpy as np
import pytest

from kicad_autorouter.routing.grid import board_to_grid, create_grid_from_model
from kicad_autorouter.routing.obstacles import build_fanout_cost, THT_EXCLUSION
from kicad_autorouter.routing.router import SECONDARY_BLOCKED

from .helpers import make_board, make_fp


def _tht_victim_board(px=0.0, py=0.0, size=2.0):
    """One THT pad (net B) on a bare board; routed net is A (no pads)."""
    from dataclasses import replace

    from kicad_autorouter.board_model import Pad, Point

    fp = make_fp("J1", "uuid-j1", px, py, ("B",), tht=True)
    pads = []
    for p in fp.pads:
        pads.append(
            Pad(
                number=p.number,
                net_name=p.net_name,
                position=p.position,
                size_mm=(size, size),
                shape="circle",
                angle_deg=0.0,
                pad_type="thru_hole",
                drill_mm=0.8,
                layers=p.layers,
            )
        )
    fp = replace(fp, pads=tuple(pads))
    return make_board([fp], edge=30.0)


def test_tht_exclusion_covers_copper_plus_track_plus_guard():
    """Every cell touching foreign copper (at routed width) is hard-excluded.

    Center sampling can miss boundary cells by up to half a diagonal, and
    a track whose centerline threads such a cell still overlaps copper by
    up to its half-width (tube111: 0.5 mm power tracks grazing foreign
    SMD pads after paying the steep halo toll). The hard-core tier must
    therefore cover copper + the routed track half-width + the sampling
    miss distance — checked here for signal (0.125) and power (0.25) widths.
    """
    import pytest

    model = _tht_victim_board()
    grid = create_grid_from_model(model, resolution_mm=0.1, margin_mm=2.0)
    res = grid.resolution_mm
    guard = res * math.sqrt(2) / 2.0
    copper_half = 1.0  # size 2.0 disc
    for track_half in (0.125, 0.25):
        secondary = build_fanout_cost(
            model, grid, "A", clearance_mm=0.2, track_half_mm=track_half)
        assert secondary.shape[0] == len(grid.layers)
        inner = copper_half + track_half + guard
        for layer in range(secondary.shape[0]):
            # Cells whose center is within copper + track + guard: excluded.
            # (Origin-independent: scan the whole pad bbox.)
            for r in range(grid.height_cells):
                for c in range(grid.width_cells):
                    from kicad_autorouter.routing.grid import grid_to_board
                    x, y = grid_to_board(grid, c, r)
                    if math.hypot(x, y) <= inner + 1e-9:
                        assert secondary[layer, r, c] >= SECONDARY_BLOCKED, (
                            f"track_half={track_half} cell {(c, r)} at dist "
                            f"{math.hypot(x, y):.4f} not hard-excluded")
            # ... and the guard must not balloon: cells well clear stay soft.
            c, r = board_to_grid(grid, inner + 0.3, 0.0)
            if 0 <= r < grid.height_cells and 0 <= c < grid.width_cells:
                assert secondary[layer, r, c] < SECONDARY_BLOCKED


def test_tht_exclusion_scales_with_track_width():
    """A wide track excludes strictly more cells than a narrow one."""
    model = _tht_victim_board()
    grid = create_grid_from_model(model, resolution_mm=0.1, margin_mm=2.0)
    narrow = build_fanout_cost(
        model, grid, "A", clearance_mm=0.2, track_half_mm=0.125)
    wide = build_fanout_cost(
        model, grid, "A", clearance_mm=0.2, track_half_mm=0.25)
    assert (wide >= SECONDARY_BLOCKED).sum() > (
        narrow >= SECONDARY_BLOCKED).sum()


def test_tht_exclusion_value_still_impassable():
    """The guarded tier keeps impassable semantics for the search."""
    assert THT_EXCLUSION >= SECONDARY_BLOCKED
