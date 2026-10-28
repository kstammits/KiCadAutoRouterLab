"""Degenerate courtyards and unconnected copper.

Beads-2m findings, reproduced synthetically (no 1.4MB fixture):

- Some chips carry no real outline: the Fab layer holds only pin-marker
  dots. The outline gate falls back to the pad-copper hull and the run
  proposal names the offenders. Coverage gaps (partial outlines, pins
  outside a closed courtyard) keep their drawn body — reporting only.
- Pots with no-net mounting pins wandered on long runs: unconnected pads
  were excluded from pad nodes, leaving a collinear netted subset that
  degenerates the Kabsch rigid fit. Mounted copper joins as geometry-only
  nodes (no Coulomb charge); attraction still keys on net_name.

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
    # Convex hull of the two 1x1 copper squares (x=±1) + 0.1 margin band:
    # 3.2 x 1.2 rect minus rounded corners, bounds grown by the margin.
    assert tuple(round(v, 9) for v in poly.bounds) == (-1.6, -0.6, 1.6, 0.6)
    assert poly.area == pytest.approx(3.2 * 1.2 - 0.04 * (4 - math.pi), rel=0.02)


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


def test_mass_floored_by_pad_hull():
    dots = courtyard_bbox_area_m2(_dots_chip())
    # Hull of the two 1x1 copper squares: 3.0 mm^2, rotation-invariant.
    assert dots == pytest.approx(3.0 * 1e-6, rel=1e-9)
    honest = courtyard_bbox_area_m2(make_fp(
        "U2", "uuid-u2", 0.0, 0.0, ("N1", "N2"),
        layer="F.Cu", hw=3.0, hh=2.0, pad_dx=1.0))
    assert honest == pytest.approx(6.0 * 4.0 * 1e-6, rel=1e-9)


def test_mass_invariant_under_rotation():
    """Mass depends on copper size, never on placement angle."""
    from kicad_autorouter.board_model import Pad, apply_deltas

    def _fp():
        pads = tuple(
            Pad(number=str(k + 1), net_name=f"N{k}", position=Point(x, 0.0),
                size_mm=(2.0, 1.0), shape="rect", angle_deg=0.0,
                pad_type="smd", drill_mm=0.0, layers=("F.Cu",))
            for k, x in enumerate((-1.5, 1.5))
        )
        return Footprint(
            ref="R1", footprint_id="test:R", layer="F.Cu",
            x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=pads,
            courtyard=(), locked=False, uuid="uuid-r1",
            ghost=False, ghost_attractions=(),
        )

    fp = _fp()
    # Two 2x1 pads at x=±1.5: hull spans 5.0 x 1.0.
    assert courtyard_bbox_area_m2(fp) == pytest.approx(5.0 * 1e-6, rel=1e-9)
    turned = next(
        f for f in apply_deltas(_board(fp), {"uuid-r1": (0.0, 0.0, 45.0)}).footprints
        if f.uuid == "uuid-r1"
    )
    assert turned.angle_deg == pytest.approx(45.0)
    assert courtyard_bbox_area_m2(turned) == pytest.approx(
        courtyard_bbox_area_m2(fp), rel=1e-9)


def test_diagonal_part_body_follows_pads():
    """D1-analog: SOD123-style part at 117°, dots outline, pads diagonal.

    The hull body must contain both pad coppers and sit rotated (tighter
    than the axis-aligned sprawl), and must track a 90° placement rotation.
    """
    import numpy as np

    from kicad_autorouter.board_model import Pad, apply_deltas
    from kicad_autorouter.placement.contact import pad_copper_polygon
    from kicad_autorouter.placement.model import _collect_pad_nodes

    pads = tuple(
        Pad(number=n, net_name=net, position=Point(x, y),
            size_mm=(1.4, 1.4), shape="rect", angle_deg=117.0,
            pad_type="smd", drill_mm=0.0, layers=("F.Cu",))
        for n, net, x, y in (("A", "N1", -0.9, -1.7), ("C", "N2", 0.9, 1.7))
    )
    fp = Footprint(
        ref="D1", footprint_id="test:SOD123", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=117.0, pads=pads,
        courtyard=_dots_courtyard(), locked=False, uuid="uuid-d1",
        ghost=False, ghost_attractions=(),
    )
    assert courtyard_is_degenerate(fp)
    body = _footprint_courtyard_polygon(fp)
    for p in pads:
        assert body.covers(pad_copper_polygon(p)), "body must contain pad copper"
    # The old axis-aligned center box missed copper entirely; the hull's
    # long axis follows the diagonal pad line (~62°).
    mrr = body.minimum_rotated_rectangle
    ring = list(mrr.exterior.coords)
    edges = [(ring[i], ring[i + 1]) for i in range(4)]
    lens = [math.dist(a, b) for a, b in edges]
    a, b = edges[lens.index(max(lens))]
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    assert min(abs(ang - 62.1), abs(ang - 152.1)) < 5.0

    model = _board(fp)
    # 90° placement rotation: the body tracks it (bounds swap).
    moved = apply_deltas(model, {"uuid-d1": (0.0, 0.0, 90.0)})
    pads2, _, uuid_to_idx2, _, _ = _collect_pad_nodes(moved)
    pos2 = np.array([(p.position.x_mm, p.position.y_mm) for p in pads2])
    mfp = next(f for f in moved.footprints if f.uuid == "uuid-d1")
    turned = _footprint_courtyard_polygon(
        mfp, pad_positions=pos2, pad_indices=uuid_to_idx2["uuid-d1"])
    a = body.bounds
    b = turned.bounds
    assert (b[2] - b[0]) == pytest.approx(a[3] - a[1], rel=0.05)
    assert (b[3] - b[1]) == pytest.approx(a[2] - a[0], rel=0.05)


def test_partial_outline_stands_but_reports():
    """IC1-analog: two disjoint diagonal strokes, diagonal pad row.

    The drawn segment bbox stands as the body (drawn geometry is not
    reshaped), but the ref is reported: the box neither matches the pins'
    diagonal nor contains them.
    """
    segs = (
        (Point(-1.0, -1.0), Point(0.5, 1.5)),
        (Point(1.5, -0.5), Point(2.5, -2.0)),
    )
    pads = tuple(
        Pad(number=n, net_name=net, position=Point(x, y),
            size_mm=(0.4, 1.0), shape="rect", angle_deg=45.0,
            pad_type="smd", drill_mm=0.0, layers=("F.Cu",))
        for n, net, x, y in (("1", "N1", -1.5, -1.5), ("2", "N2", 2.0, 2.5))
    )
    fp = Footprint(
        ref="IC1", footprint_id="test:MSOP", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=45.0, pads=pads,
        courtyard=segs, locked=False, uuid="uuid-ic1",
        ghost=False, ghost_attractions=(),
    )
    assert courtyard_is_degenerate(fp)
    body = _footprint_courtyard_polygon(fp)
    # Drawn box stands: hull of the two strokes, no pad ears added.
    assert body.bounds == pytest.approx((-1.1, -2.1, 2.6, 1.6))


def test_pins_outside_closed_outline_stands_but_reports():
    """R23-analog: closed outline missing a pin row.

    The closed body stands (drawn geometry is not reshaped — protruding
    THT pins already collide as extenders), but the ref is reported so the
    library drawing gets fixed at the source.
    """
    pads = tuple(
        Pad(number=n, net_name=net, position=Point(x, y),
            size_mm=(1.9, 1.9), shape="rect", angle_deg=0.0,
            pad_type="thru_hole", drill_mm=1.0, layers=("*.Cu", "*.Mask"))
        for n, net, x, y in (("1", "S1", 0.0, 0.0), ("2", "S2", -2.0, -2.0))
    ) + (
        Pad(number="M", net_name=None, position=Point(8.0, 8.0),
            size_mm=(2.5, 2.5), shape="oval", angle_deg=0.0,
            pad_type="thru_hole", drill_mm=1.2, layers=("*.Cu", "*.Mask")),
    )
    fp = Footprint(
        ref="R23", footprint_id="test:POT", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=pads,
        courtyard=make_rect_courtyard(0.0, 0.0, 5.0, 5.0),
        locked=False, uuid="uuid-r23",
        ghost=False, ghost_attractions=(),
    )
    assert courtyard_is_degenerate(fp)
    body = _footprint_courtyard_polygon(fp)
    assert body.area == pytest.approx(10.0 * 10.0, rel=1e-9)


def test_deliberate_approximation_stands_unreported():
    """Disc-cap analog: drawn circle slightly smaller than the pads.

    Pads at x=±0.8 (copper to ±1.3) against a r=1 circle: 0.6mm^2 of
    copper outside the drawing stays under the 1mm^2 bar, so the outline
    stands and the ref is not reported. (Multi-mm^2 overhangs like R23's
    missing pin row are reported — see above.)
    """
    import math as _math

    segs = []
    for i in range(8):
        a1 = 2 * _math.pi * i / 8
        a2 = 2 * _math.pi * (i + 1) / 8
        segs.append((Point(_math.cos(a1), _math.sin(a1)),
                     Point(_math.cos(a2), _math.sin(a2))))
    pads = tuple(
        Pad(number=n, net_name=net, position=Point(x, 0.0),
            size_mm=(1.0, 1.0), shape="rect", angle_deg=0.0,
            pad_type="smd", drill_mm=0.0, layers=("F.Cu",))
        for n, net, x in (("1", "N1", -0.8), ("2", "N2", 0.8))
    )
    fp = Footprint(
        ref="C1", footprint_id="test:DISC", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=pads,
        courtyard=tuple(segs), locked=False, uuid="uuid-c1",
        ghost=False, ghost_attractions=(),
    )
    assert not courtyard_is_degenerate(fp)
    body = _footprint_courtyard_polygon(fp)
    # 8-seg octagon, area 2*sqrt(2) — the drawing stands as drawn.
    assert body.area == pytest.approx(2 * _math.sqrt(2), rel=0.02)


def test_padless_footprint_never_degenerate():
    """Mounting-hole-style footprints with no pads cannot be degenerate."""
    fp = Footprint(
        ref="MH1", footprint_id="test:MOUNT", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=(),
        courtyard=(), locked=False, uuid="uuid-mh1",
        ghost=False, ghost_attractions=(),
    )
    assert not courtyard_is_degenerate(fp)
    body = _footprint_courtyard_polygon(fp)
    assert body.area == pytest.approx(1.0)


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
