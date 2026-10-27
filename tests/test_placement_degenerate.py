"""Degenerate courtyards and unconnected copper.

Beads-2m findings, reproduced synthetically (no 1.4MB fixture):

- Some chips carry no real outline: the Fab layer holds only pin-marker
  dots. The fallback chain used to prefer those dots (0.17mm^2 bodies, no
  wall repulsion, invisible in the viewer). The outline gate now falls back
  to the pad bounding box and the run proposal names the offenders.
- Pots with no-net mounting pins wandered on long runs: unconnected pads
  were excluded from pad nodes, leaving a collinear netted subset that
  degenerates the Kabsch rigid fit. All copper is a node now (ghosts
  excepted); attraction still keys on net_name.

Markers: unit
"""

import math
from dataclasses import replace

import pytest

from kicad_autorouter.board_model import BoardModel, Footprint, Pad, Point
from kicad_autorouter.mass import courtyard_bbox_area_m2
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.placement.contact import (
    _footprint_courtyard_polygon,
    courtyard_is_degenerate,
)
from kicad_autorouter.placement.model import (
    _collect_pad_nodes,
    _find_decap_pad_indices,
)

from .helpers import make_fp, make_rect_courtyard


def _dots_courtyard():
    """Five disjoint 0.2mm Fab-style marker squares (IC2 pattern)."""
    segs = []
    for dx in (-2.0, -1.0, 0.0, 1.0, 2.0):
        s = 0.1
        c = [
            Point(dx - s, 2.8 - s), Point(dx + s, 2.8 - s),
            Point(dx + s, 2.8 + s), Point(dx - s, 2.8 + s),
            Point(dx - s, 2.8 - s),
        ]
        segs.extend(zip(c, c[1:]))
    return tuple(segs)


def _dots_chip():
    base = make_fp("U1", "uuid-u1", 0.0, 0.0, ("N1", "N2"),
                   layer="F.Cu", hw=3.0, hh=2.0, pad_dx=1.0)
    return replace(base, courtyard=_dots_courtyard())


def _board(*fps):
    edge = 30.0
    by_ref = {fp.ref: fp for fp in fps}
    regions = {fp.uuid: 0 for fp in fps}
    return BoardModel(
        footprints=fps,
        keepout_zones=(),
        nets={},
        by_ref=by_ref,
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region=regions,
    )


def _pot_fp():
    """ALPS-style pot: 3 collinear signal pads + 2 wide no-net mounts."""
    pads = (
        Pad(number="1", net_name="S1", position=Point(-1.5, 0.0),
            size_mm=(1.9, 1.9), shape="rect", angle_deg=0.0,
            pad_type="thru_hole", drill_mm=1.0, layers=("*.Cu", "*.Mask")),
        Pad(number="2", net_name="S2", position=Point(0.0, 0.0),
            size_mm=(1.9, 1.9), shape="rect", angle_deg=0.0,
            pad_type="thru_hole", drill_mm=1.0, layers=("*.Cu", "*.Mask")),
        Pad(number="3", net_name="S3", position=Point(1.5, 0.0),
            size_mm=(1.9, 1.9), shape="rect", angle_deg=0.0,
            pad_type="thru_hole", drill_mm=1.0, layers=("*.Cu", "*.Mask")),
        Pad(number="P$4", net_name=None, position=Point(-3.0, 2.5),
            size_mm=(4.0, 2.0), shape="oval", angle_deg=0.0,
            pad_type="thru_hole", drill_mm=1.2, layers=("*.Cu", "*.Mask")),
        Pad(number="P$5", net_name=None, position=Point(3.0, 2.5),
            size_mm=(4.0, 2.0), shape="oval", angle_deg=0.0,
            pad_type="thru_hole", drill_mm=1.2, layers=("*.Cu", "*.Mask")),
    )
    return Footprint(
        ref="R1", footprint_id="test:POT", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=pads,
        courtyard=make_rect_courtyard(0.0, 0.0, 5.0, 3.5),
        locked=False, uuid="uuid-pot", ghost=False, ghost_attractions=(),
    )


def test_dots_outline_is_degenerate():
    fp = _dots_chip()
    assert courtyard_is_degenerate(fp)
    poly = _footprint_courtyard_polygon(fp)
    # Pad-center bbox (pads at x=±1, y=0) + 0.1 margin: 2.2 x 0.2.
    assert poly.area == pytest.approx(2.2 * 0.2, rel=1e-9)


def test_honest_outline_passes_gate():
    fp = make_fp("U2", "uuid-u2", 0.0, 0.0, ("N1", "N2"),
                 layer="F.Cu", hw=3.0, hh=2.0, pad_dx=1.0)
    assert not courtyard_is_degenerate(fp)
    poly = _footprint_courtyard_polygon(fp)
    assert poly.area == pytest.approx(6.0 * 4.0, rel=1e-9)


def test_degenerate_bodies_surfaces_in_proposal():
    model = _board(_dots_chip())
    prop = run_placement(model, PlacementParams(max_iterations=5))
    assert "U1" in prop.degenerate_bodies


def test_mass_floored_by_pad_bbox():
    dots = courtyard_bbox_area_m2(_dots_chip())
    # Dots span x -2.1..2.1, y 2.7..2.9; pads span more: max() takes pads.
    honest = courtyard_bbox_area_m2(make_fp(
        "U2", "uuid-u2", 0.0, 0.0, ("N1", "N2"),
        layer="F.Cu", hw=3.0, hh=2.0, pad_dx=1.0))
    assert honest == pytest.approx(6.0 * 4.0 * 1e-6, rel=1e-9)
    assert dots >= (2.0 * 1.0 + 0.2) * (1.0 + 0.2) * 1e-6


def test_no_net_pads_are_nodes():
    model = _board(_pot_fp())
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(model)
    assert len(uuid_to_idx["uuid-pot"]) == 5
    assert [p.number for p in pads] == ["1", "2", "3", "P$4", "P$5"]


def test_ghost_no_net_pads_stay_out():
    pot = _pot_fp()
    ghost = replace(pot, ref="GHOST_W", uuid="uuid-ghost",
                    ghost=True, ghost_attractions=())
    edge = 30.0
    model = BoardModel(
        footprints=(pot,),
        ghost_footprints=(ghost,),
        keepout_zones=(),
        nets={},
        by_ref={pot.ref: pot, ghost.ref: ghost},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={pot.uuid: 0, ghost.uuid: 0},
    )
    _, _, uuid_to_idx, _, pad_is_ghost = _collect_pad_nodes(model)
    assert len(uuid_to_idx["uuid-pot"]) == 5
    # Ghost keeps netted-only membership: its 3 signal pads are nodes,
    # its 2 no-net mounts are not.
    assert len(uuid_to_idx["uuid-ghost"]) == 3
    assert sum(pad_is_ghost) == 3


def test_no_net_mount_does_not_break_decap():
    pads = [
        Pad(number="1", net_name="VIN", position=Point(-1.0, 0.0),
            size_mm=(1.0, 1.0), shape="rect", angle_deg=0.0,
            pad_type="smd", drill_mm=0.0, layers=("F.Cu",)),
        Pad(number="2", net_name="GND", position=Point(1.0, 0.0),
            size_mm=(1.0, 1.0), shape="rect", angle_deg=0.0,
            pad_type="smd", drill_mm=0.0, layers=("F.Cu",)),
        Pad(number="3", net_name=None, position=Point(0.0, 2.0),
            size_mm=(1.0, 1.0), shape="rect", angle_deg=0.0,
            pad_type="smd", drill_mm=0.0, layers=("F.Cu",)),
    ]
    assert _find_decap_pad_indices(pads, [0, 0, 0], {0: "C7"}) == {0, 1}


def test_no_net_pads_carry_no_charge():
    """Unconnected copper is Coulomb-neutral (geometry only).

    Two pads 5mm apart (beyond any halo, so no contact): with one pad
    uncharged the repulsion component is exactly zero on both pads; with
    both charged it is nonzero.
    """
    import numpy as np

    from kicad_autorouter.placement import PlacementParams
    from kicad_autorouter.placement.force import (
        ForceContext,
        _compute_total_force,
    )

    pos = np.array([[0.0, 0.0], [5.0, 0.0]])
    movable = np.array([True, True])
    no_contact = np.zeros((2, 2), dtype=bool)
    empty = np.array([], dtype=int)
    params = PlacementParams()

    def _run(charged):
        ctx = ForceContext(
            fp_uuids=[], fp_local_to_rows=[],
            fp_movable_arr=movable, pad_to_fp_local=[0, 0],
            poly_cache={}, cache_rebuild_threshold_mm=0.5,
            cache_rebuild_threshold_rad=0.05, pad_movable=movable,
            pad_charged=np.array(charged, dtype=bool),
        )
        _, comps = _compute_total_force(
            pos, 2, movable, params, False, empty, empty,
            np.array([], dtype=float), [],
            no_contact, return_components=True, ctx=ctx,
        )
        return comps["repulsion"]

    assert np.all(_run([True, False]) == 0.0)
    assert np.all(_run([False, True]) == 0.0)
    both = _run([True, True])
    assert np.linalg.norm(both, axis=1).min() > 0.0


def test_pot_stays_in_region_on_long_run():
    pot = _pot_fp()
    shover = make_fp("R9", "uuid-r9", 0.0, 6.0, ("N9", "N10"),
                     layer="F.Cu", hw=2.0, hh=1.5, pad_dx=1.0)
    model = _board(pot, shover)
    prop = run_placement(
        model, PlacementParams(max_iterations=500, wall_halo_m=0.004))
    assert prop.wall_violations == ()
    for fp in (pot, shover):
        dx, dy, _ = prop.deltas.get(fp.uuid, (0.0, 0.0, 0.0))
        assert abs(fp.x_mm + dx) < 28.0
        assert abs(fp.y_mm + dy) < 28.0
