"""Layer-aware courtyard collision: same side collides, opposite sides stack.

Placement used to be 2D-projected (layer-blind): a B.Cu part repelled F.Cu
parts as if coplanar. The courtyard model is now layer-aware:

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

from kicad_autorouter.board_model import (
    BoardModel,
    Footprint,
    NetConnection,
    Pad,
    Point,
)
from kicad_autorouter.placement import (
    PlacementParams,
    _collect_pad_nodes,
    _footprint_courtyard_polygon,
    run_placement,
)
from kicad_autorouter.board_model import apply_deltas


def _rect_courtyard(hw: float, hh: float):
    c = [Point(-hw, -hh), Point(hw, -hh), Point(hw, hh), Point(-hw, hh)]
    return tuple((c[i], c[(i + 1) % 4]) for i in range(4))


def _two_part_board(layer_b: str = "F.Cu", tht_b: bool = False, shared_net: bool = False):
    """Two 6x4mm parts 1mm apart (heavy courtyard overlap), 2 pads each."""
    def fp(ref, uuid, x, layer, tht, net_a, net_b):
        pads = tuple(
            Pad(
                number=str(k + 1),
                net_name=n,
                position=Point(x + (k - 0.5) * 2.0, 0.0),
                size_mm=(1.0, 1.0),
                pad_type="thru_hole" if tht else "smd",
                drill_mm=0.8 if tht else 0.0,
                layers=("F.Cu", "B.Cu") if tht else ("F.Cu",),
            )
            for k, n in enumerate((net_a, net_b))
        )
        return Footprint(
            ref=ref, footprint_id="test:R", layer=layer, x_mm=x, y_mm=0.0,
            angle_deg=0.0, pads=pads, courtyard=_rect_courtyard(3.0, 2.0),
            uuid=uuid,
        )

    fa = fp("A", "uuid-a", 0.0, "F.Cu", False, "SHARED" if shared_net else "N1", "N2")
    fb = fp("B", "uuid-b", 1.0, layer_b, tht_b, "SHARED" if shared_net else "N3", "N4")
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
    """Courtyard intersection area at posed positions (sim-consistent geometry)."""
    moved = apply_deltas(model, deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    polys = []
    for uuid in ("uuid-a", "uuid-b"):
        fp = next(f for f in moved.footprints if f.uuid == uuid)
        polys.append(_footprint_courtyard_polygon(
            fp, pad_positions=pos, pad_indices=uuid_to_idx.get(uuid, [])))
    if polys[0].is_empty or polys[1].is_empty:
        return 0.0
    return polys[0].intersection(polys[1]).area


def _centroid_gap(model: BoardModel, deltas: dict) -> float:
    ca = model.by_ref["A"]
    cb = model.by_ref["B"]
    dxa, dya, _ = deltas.get("uuid-a", (0.0, 0.0, 0.0))
    dxb, dyb, _ = deltas.get("uuid-b", (0.0, 0.0, 0.0))
    return math.hypot((ca.x_mm + dxa) - (cb.x_mm + dxb), (ca.y_mm + dya) - (cb.y_mm + dyb))


PARAMS = PlacementParams(
    max_iterations=10,
    convergence_eps_mm=1e-12,
    repulsion_kr=100.0,
    attraction_ka=0.05,
    ideal_length_mm=25.0,
    courtyard_repulsion_kc=10000.0,
    courtyard_halo_mm=3.0,
    boundary_repulsion_kb=1.0,  # negligible: parts start at board center
    rigid_stiffness=5e5,
)


def test_same_layer_overlap_resolves():
    model = _two_part_board()
    before = _overlap_area(model, {})
    prop = run_placement(model, PARAMS)
    after = _overlap_area(model, prop.deltas)
    assert before > 10.0  # heavy initial overlap (6x4 rects 1mm apart)
    assert after == 0.0  # pushed fully apart


def test_cross_layer_overlap_tolerated():
    model = _two_part_board(layer_b="B.Cu")
    before = _overlap_area(model, {})
    prop = run_placement(model, PARAMS)
    after = _overlap_area(model, prop.deltas)
    assert before > 10.0
    # No courtyard force across sides: overlap essentially unchanged
    # (pad repulsion alone cannot separate 6x4 bodies in 10 iters).
    assert after > before * 0.5


def test_tht_collides_across_layers():
    model = _two_part_board(layer_b="B.Cu", tht_b=True)
    prop = run_placement(model, PARAMS)
    assert _overlap_area(model, prop.deltas) == 0.0


def test_cross_layer_shared_net_edge_built():
    """Attraction edge is built for cross-layer pads sharing a net.

    Proves the attraction graph is layer-blind: F.Cu and B.Cu pads on the
    same net produce an edge regardless of layer.
    """
    from kicad_autorouter.placement import _build_net_edges_pad_level
    from kicad_autorouter.placement import _collect_pad_nodes

    model = _two_part_board(layer_b="B.Cu", shared_net=True)
    pads, pad_to_fp_idx, _, _, _ = _collect_pad_nodes(model)
    src, dst = _build_net_edges_pad_level(pads, pad_to_fp_idx)
    assert len(src) == 1
    assert pad_to_fp_idx[src[0]] == 0  # F.Cu footprint
    assert pad_to_fp_idx[dst[0]] == 1  # B.Cu footprint


def test_cross_layer_distinct_nets_no_edge():
    """No attraction edge between cross-layer pads on different nets."""
    from kicad_autorouter.placement import _build_net_edges_pad_level
    from kicad_autorouter.placement import _collect_pad_nodes

    model = _two_part_board(layer_b="B.Cu", shared_net=False)
    pads, pad_to_fp_idx, _, _, _ = _collect_pad_nodes(model)
    src, dst = _build_net_edges_pad_level(pads, pad_to_fp_idx)
    assert len(src) == 0
