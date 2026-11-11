"""Layer-aware courtyard collision: same side collides, opposite sides stack.

Courtyard collision is layer-aware; a B.Cu part does not repel F.Cu parts as
if coplanar:

- same placement layer -> collide (overlap + halo), as before;
- opposite sides, both SMD -> tolerated (no courtyard force);
- any pair involving through-hole pads -> collides (protruding leads);
- attraction/repulsion/rigid stay layer-blind (cross-layer electrical
  nearness keeps working through springs).

These tests use minimal two-footprint boards with heavy initial overlap so
the courtyard force dominates pad repulsion. Thresholds were calibrated by
running (see asserts); the properties are structural, not numeric.
"""

import math

import numpy as np
import pytest
from dataclasses import replace

from kicad_autorouter.board_model import (
    BoardModel,
    Point,
)
from kicad_autorouter.placement import (
    PlacementParams,
    run_placement,
)

from .helpers import make_fp, overlap_area


pytestmark = pytest.mark.unit


def _two_part_board(layer_b: str = "F.Cu", tht_b: bool = False, shared_net: bool = False):
    """Two 6x4mm parts 1mm apart in x, staggered 2mm in y (heavy overlap).

    The y-stagger keeps the pair in a generic position: exactly collinear
    head-on pairs are a measure-zero symmetric configuration where a
    deterministic local solver's symmetric manifold is invariant (pads
    slosh symmetrically, centroids lock, and the pair can re-stack instead
    of glancing off). Real boards are never bit-symmetric; the stagger
    preserves heavy initial overlap (~24mm^2) while letting the parts
    glance off each other as they would in practice.
    """
    def fp(ref, uuid, x, y, layer, tht, net_a, net_b):
        return make_fp(ref, uuid, x, y, (net_a, net_b),
                       layer=layer, footprint_id="test:R", hw=3.0, hh=2.0,
                       tht=tht,
                       pad_layers=("F.Cu", "B.Cu") if tht else ("F.Cu",),
                       pad_shape="circle", courtyard_origin=True)

    fa = fp("A", "uuid-a", 0.0, 0.0, "F.Cu", False, "SHARED" if shared_net else "N1", "N2")
    fb = fp("B", "uuid-b", 1.0, 2.0, layer_b, tht_b, "SHARED" if shared_net else "N3", "N4")
    fps = (fa, fb)
    edge = 30.0
    return BoardModel(
        footprints=fps,
        keepout_zones=(),
        nets={},
        by_ref={"A": fa, "B": fb},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={"uuid-a": 0, "uuid-b": 0},
    )


def _overlap_area(model: BoardModel, deltas: dict) -> float:
    return overlap_area(model, deltas)


def _centroid_gap(model: BoardModel, deltas: dict) -> float:
    ca = model.by_ref["A"]
    cb = model.by_ref["B"]
    dxa, dya, _ = deltas.get("uuid-a", (0.0, 0.0, 0.0))
    dxb, dyb, _ = deltas.get("uuid-b", (0.0, 0.0, 0.0))
    return math.hypot((ca.x_mm + dxa) - (cb.x_mm + dxb), (ca.y_mm + dya) - (cb.y_mm + dyb))


PARAMS = PlacementParams(
    max_iterations=30,  # 1mm/iter steps need the budget a 2mm vaulting solver did not
    convergence_eps_m=1e-15,
    repulsion_k_N_m2=1.0000000000000001e-07,
    attraction_k_N=5e-05,
    wall_k_N=0.01,
    wall_halo_m=0.003,
)


def test_same_layer_overlap_resolves():
    model = _two_part_board()
    before = _overlap_area(model, {})
    prop = run_placement(model, PARAMS)
    after = _overlap_area(model, prop.deltas)
    assert before > 10.0  # heavy initial overlap (6x4 rects 1mm apart)
    assert after == 0.0  # pushed fully apart


def test_cross_layer_no_courtyard_force():
    """Opposite-side SMD pairs feel zero courtyard force (layer gate).

    Pad repulsion alone can separate the pair within 30 iters, so overlap
    reaching 0.0 is not evidence of courtyard involvement. Assert the intent
    directly: the courtyard components are exactly zero while the pair still
    moves (via layer-blind pad repulsion).
    """
    model = _two_part_board(layer_b="B.Cu")
    before = _overlap_area(model, {})
    assert before > 10.0
    prop = run_placement(model, PARAMS)
    assert prop.deltas, "pair must still move via pad Coulomb"
    for uuid, comps in prop.force_components.items():
        assert comps["courtyard"] == (0.0, 0.0), (
            f"{uuid}: cross-layer SMD-SMD must feel no courtyard force"
        )


def test_tht_collides_across_layers():
    model = _two_part_board(layer_b="B.Cu", tht_b=True)
    prop = run_placement(model, PARAMS)
    assert _overlap_area(model, prop.deltas) == 0.0


def test_pad_copper_polygon_true_shapes():
    """Pad copper follows shape/angle, not a bounding disc.

    Rect D2-style THT pads must not be inflated to their half-diagonal, and
    a 90°-rotated rect must swap extents (R3's roundrect pads are 1.2x1.4
    at 90°). Oval is a capsule along local x.
    """
    from kicad_autorouter.board_model import Pad, Point
    from kicad_autorouter.placement.contact import pad_copper_polygon

    def _pad(shape, w, h, angle=0.0):
        return Pad(number="1", net_name=None, position=Point(0.0, 0.0),
                   size_mm=(w, h), shape=shape, angle_deg=angle,
                   pad_type="smd", drill_mm=0.0, layers=("B.Cu",))

    circ = pad_copper_polygon(_pad("circle", 1.8, 1.8))
    assert circ.area == pytest.approx(math.pi * 0.9 ** 2, rel=0.02)

    rect = pad_copper_polygon(_pad("rect", 1.8, 1.8))
    assert rect.area == pytest.approx(1.8 * 1.8, rel=1e-9)
    assert rect.bounds == pytest.approx((-0.9, -0.9, 0.9, 0.9))

    tall = pad_copper_polygon(_pad("rect", 1.2, 1.4, angle=90.0))
    minx, miny, maxx, maxy = tall.bounds
    assert (maxx - minx) == pytest.approx(1.4)
    assert (maxy - miny) == pytest.approx(1.2)

    oval = pad_copper_polygon(_pad("oval", 2.0, 1.0))
    minx, miny, maxx, maxy = oval.bounds
    assert (maxx - minx) == pytest.approx(2.0)
    assert (maxy - miny) == pytest.approx(1.0)

    rr = pad_copper_polygon(_pad("roundrect", 1.2, 1.4, angle=90.0))
    minx, miny, maxx, maxy = rr.bounds
    assert (maxx - minx) == pytest.approx(1.4)
    assert (maxy - miny) == pytest.approx(1.2)


def test_cross_layer_smd_overlap_is_legal():
    """Opposite-side SMD courtyards may overlap in plan view (stacking).

    Legality mirrors the dynamics gate: no THT copper protrudes onto the
    far side, so a heavy plan-view overlap is not a violation.
    """
    from kicad_autorouter.placement.legality import check_legality

    model = _two_part_board(layer_b="B.Cu")
    assert _overlap_area(model, {}) > 10.0
    rep = check_legality(model, {})
    assert rep.is_legal
    assert rep.overlapping_pairs == ()


def test_cross_layer_tht_pin_keepout_in_legality():
    """Cross-layer THT pairs are judged by pin copper, not courtyards.

    A B.Cu body nested inside an F.Cu THT courtyard but clear of the pin
    copper is legal; the same body sitting on a pin is illegal.
    """
    from .helpers import make_fp as _make_fp
    from kicad_autorouter.board_model import Pad
    from kicad_autorouter.placement.legality import check_legality

    def _tht_a():
        fa = _make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                      layer="F.Cu", footprint_id="test:THT", hw=3.0, hh=2.0,
                      pad_dx=1.0, pad_shape="circle")
        tht_pads = tuple(
            Pad(
                number=p.number, net_name=p.net_name, position=p.position,
                size_mm=(2.0, 2.0), shape="circle", angle_deg=0.0,
                pad_type="thru_hole", drill_mm=0.8,
                layers=("*.Cu", "*.Mask"),
            )
            for p in fa.pads
        )
        return replace(fa, pads=tht_pads)

    def _board(fb):
        fa_tht = _tht_a()
        edge = 30.0
        return BoardModel(
            footprints=(fa_tht, fb),
            keepout_zones=(),
            nets={},
            by_ref={"A": fa_tht, "B": fb},
            edge_cuts=(
                (Point(-edge, -edge), Point(edge, -edge)),
                (Point(edge, -edge), Point(edge, edge)),
                (Point(edge, edge), Point(-edge, edge)),
                (Point(-edge, edge), Point(-edge, -edge)),
            ),
            footprint_region={"uuid-a": 0, "uuid-b": 0},
        )

    # B nested under A's courtyard (2.5mm^2 plan overlap) but 0.09mm clear
    # of the +x pin copper (r=1.41 at (1,0); B body bottom edge y=1.5).
    fb_clear = _make_fp("B", "uuid-b", 1.0, 3.5, ("N3", "N4"),
                        layer="B.Cu", footprint_id="test:R", hw=3.0, hh=2.0,
                        pad_shape="rect")
    model = _board(fb_clear)
    assert _overlap_area(model, {}) > 1.0
    rep = check_legality(model, {})
    assert rep.is_legal, rep.overlapping_pairs

    # Slide B onto A's +x pin: pin copper now inside B's courtyard.
    fb_on_pin = _make_fp("B", "uuid-b", 1.0, 0.0, ("N3", "N4"),
                         layer="B.Cu", footprint_id="test:R", hw=3.0, hh=2.0,
                         pad_shape="rect")
    rep2 = check_legality(_board(fb_on_pin), {})
    assert not rep2.is_legal
    assert len(rep2.overlapping_pairs) == 1


def test_tube111_d2_r3_cross_layer_accepted():
    """tube111 input pose: D2 (F.Cu THT) vs R3 (B.Cu SMD) is not a violation.

    R3 sits inside D2's plan-view courtyard but ~3mm clear of D2's pin
    copper, so the cross-layer pin keep-out passes. The board as a whole
    is still illegal (same-layer squeezes elsewhere) and no cross-layer
    pair may remain flagged.
    """
    from pathlib import Path

    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.placement.legality import check_legality
    from kicad_autorouter.sexpr import parse_file

    model = board_model(parse_file(Path(__file__).parent / "fixtures" / "tube111.kicad_pcb"))
    rep = check_legality(model, {})
    refs = {f.uuid: (f.ref, f.layer) for f in model.footprints}
    d2 = next(f.uuid for f in model.footprints if f.ref == "D2")
    r3 = next(f.uuid for f in model.footprints if f.ref == "R3")
    assert not any({a, b} == {d2, r3} for a, b, _ in rep.overlapping_pairs)
    assert not rep.is_legal  # same-layer squeezes elsewhere still count
    assert all(
        refs[a][1] == refs[b][1] for a, b, _ in rep.overlapping_pairs
    ), "only same-layer pairs may be flagged"


def test_cross_layer_shared_net_edge_built():
    """Attraction edge is built for cross-layer pads sharing a net.

    Proves the attraction graph is layer-blind: F.Cu and B.Cu pads on the
    same net produce an edge regardless of layer.
    """
    from kicad_autorouter.placement import _build_net_edges_pad_level
    from kicad_autorouter.placement import _collect_pad_nodes

    model = _two_part_board(layer_b="B.Cu", shared_net=True)
    pads, pad_to_fp_idx, _, _, _ = _collect_pad_nodes(model)
    src, dst, weights = _build_net_edges_pad_level(pads, pad_to_fp_idx)
    assert len(src) == 1
    assert pad_to_fp_idx[src[0]] == 0  # F.Cu footprint
    assert pad_to_fp_idx[dst[0]] == 1  # B.Cu footprint


def test_cross_layer_pin_halo_is_tunable():
    """THT pin vs far-side body near-miss uses cross_layer_pin_halo_m.

    Static force probe (max_iterations=0): an F.Cu THT pin sitting 0.3mm
    off a B.Cu body feels courtyard push with the 0.7mm default, and
    nothing with 0.0 (bodies on opposite sides never collide).
    """
    pin_r = 0.5 * math.hypot(2.0, 2.0) + 0.5  # THT pin radius (force.py)
    gap = 0.3
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 layer="F.Cu", footprint_id="test:THT", hw=3.0, hh=2.0,
                 tht=True, pad_dx=1.0, pad_shape="circle",
                 courtyard_origin=True)
    # +x pin at x=1; park the 6-wide B body one gap past its circle.
    bx = 1.0 + pin_r + gap + 3.0
    fb = make_fp("B", "uuid-b", bx, 0.0, ("N3", "N4"),
                 layer="B.Cu", footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_shape="rect", courtyard_origin=True)
    edge = 30.0
    model = BoardModel(
        footprints=(fa, fb),
        keepout_zones=(),
        nets={},
        by_ref={"A": fa, "B": fb},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={"uuid-a": 0, "uuid-b": 0},
    )
    probe = replace(PARAMS, max_iterations=0)
    on = run_placement(model, probe)
    for uuid in ("uuid-a", "uuid-b"):
        fx, fy = on.force_components[uuid]["courtyard"]
        assert (fx, fy) != (0.0, 0.0), f"{uuid}: pin halo must push"
    off = run_placement(model, replace(probe, cross_layer_pin_halo_m=0.0))
    for uuid in ("uuid-a", "uuid-b"):
        assert off.force_components[uuid]["courtyard"] == (0.0, 0.0), (
            f"{uuid}: halo 0 must silence cross-layer pins"
        )


def test_cross_layer_distinct_nets_no_edge():
    """No attraction edge between cross-layer pads on different nets."""
    from kicad_autorouter.placement import _build_net_edges_pad_level
    from kicad_autorouter.placement import _collect_pad_nodes

    model = _two_part_board(layer_b="B.Cu", shared_net=False)
    pads, pad_to_fp_idx, _, _, _ = _collect_pad_nodes(model)
    src, dst, weights = _build_net_edges_pad_level(pads, pad_to_fp_idx)
    assert len(src) == 0
