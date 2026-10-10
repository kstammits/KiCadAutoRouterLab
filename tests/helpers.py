"""Shared builders for routing/placement tests.

Consolidates the ~10 copy-pasted ``Pad(...)`` spells, 6 rect-courtyard
builders, 7 two-pad footprint factories, and plan-view geometry one-liners
previously duplicated across test_routing_crossing, test_routing_tht_free_via,
test_routing_obstacles, test_placement, test_placement_pairs and
test_placement_layers.

Convention (matches all prior call sites): pads are built at the origin
and placed with ``dataclasses.replace(pad, position=...)``; footprints
take board-space ``(x, y)`` with pads at ``±pad_dx``. Placement-style
origin-centered courtyards use ``courtyard_origin=True``.
"""

from __future__ import annotations

from dataclasses import replace  # noqa: F401  (re-exported for call sites)
from typing import Optional, Sequence, Tuple

from shapely.geometry import LineString
from shapely.geometry import Point as ShapelyPoint

from kicad_autorouter.board_model import Footprint, Pad, Point, tracks


def make_smd_pad(
    number: str,
    net_name: Optional[str],
    layer: str = "F.Cu",
    size_mm: Tuple[float, float] = (1.0, 1.0),
    shape: str = "rect",
    pad_layers: Optional[Tuple[str, ...]] = None,
) -> Pad:
    """2D SMD pad at the origin; caller sets ``position`` via ``replace``.

    Default layers are the full solder triple for ``layer``; pass
    ``pad_layers`` to preserve exact legacy spells (e.g. ``("F.Cu",)``).
    """
    layers = (
        ("F.Cu", "F.Paste", "F.Mask")
        if layer == "F.Cu"
        else ("B.Cu", "B.Paste", "B.Mask")
    )
    if pad_layers is not None:
        layers = pad_layers
    return Pad(
        number=number,
        net_name=net_name,
        position=Point(0.0, 0.0),
        size_mm=size_mm,
        shape=shape,
        angle_deg=0.0,
        pad_type="smd",
        drill_mm=0.0,
        layers=layers,
    )


def make_tht_pad(
    number: str,
    net_name: Optional[str],
    drill_mm: float = 0.8,
    size_mm: Tuple[float, float] = (2.0, 2.0),
) -> Pad:
    """Through-hole pad at the origin; caller sets ``position`` via ``replace``."""
    return Pad(
        number=number,
        net_name=net_name,
        position=Point(0.0, 0.0),
        size_mm=size_mm,
        shape="circle",
        angle_deg=0.0,
        pad_type="thru_hole",
        drill_mm=drill_mm,
        layers=("*.Cu", "*.Mask"),
    )


def make_rect_courtyard(
    cx: float, cy: float, hw: float = 1.5, hh: float = 1.0
):
    """Closed 4-segment rect outline (head-to-tail, valid ring)."""
    corners = [
        Point(cx - hw, cy - hh), Point(cx + hw, cy - hh),
        Point(cx + hw, cy + hh), Point(cx - hw, cy + hh),
        Point(cx - hw, cy - hh),
    ]
    return tuple((corners[i], corners[i + 1]) for i in range(4))


def make_fp(
    ref: str,
    uuid: str,
    x: float,
    y: float = 0.0,
    nets: Sequence[Optional[str]] = ("N1", "N2"),
    *,
    layer: str = "F.Cu",
    footprint_id: str = "Test:R_0805",
    hw: float = 1.5,
    hh: float = 1.0,
    pad_dx: float = 1.0,
    tht: bool = False,
    pad_layers: Optional[Tuple[str, ...]] = None,
    pad_shape: str = "rect",
    courtyard_origin: bool = False,
    locked: bool = False,
) -> Footprint:
    """N-pad footprint with pads spread ``±pad_dx`` around ``(x, y)``.

    ``courtyard_origin=True`` centers the outline at the origin
    (placement-style local coords); otherwise at ``(x, y)`` (board coords).
    ``tht=True`` makes all pads through-hole (drill 0.8, F.Cu+B.Cu layers).
    """
    n = len(nets)
    pads = []
    for k, net in enumerate(nets):
        px = x + (k - (n - 1) / 2.0) * 2.0 * pad_dx if n > 1 else x
        if tht:
            pad = replace(
                make_tht_pad(str(k + 1), net), position=Point(px, y)
            )
            if pad_layers is not None:
                pad = replace(pad, layers=pad_layers)
        else:
            pad = replace(
                make_smd_pad(str(k + 1), net, layer, pad_layers=pad_layers, shape=pad_shape),
                position=Point(px, y),
            )
        pads.append(pad)
    ccx, ccy = (0.0, 0.0) if courtyard_origin else (x, y)
    return Footprint(
        ref=ref,
        footprint_id=footprint_id,
        layer=layer,
        x_mm=x,
        y_mm=y,
        angle_deg=0.0,
        pads=tuple(pads),
        courtyard=make_rect_courtyard(ccx, ccy, hw, hh),
        locked=locked,
        uuid=uuid,
        ghost=False,
        ghost_attractions=(),
    )


def plan_segments(pcb_tree, net: str, layer: str) -> list:
    """Plan-view LineStrings of a net's tracks on one copper layer."""
    segs = []
    for tr in tracks(pcb_tree):
        if tr.net_name == net and tr.layer == layer:
            segs.append(LineString([
                (tr.start.x_mm, tr.start.y_mm),
                (tr.end.x_mm, tr.end.y_mm),
            ]))
    return segs


def closest_approach(segs: list, x_mm: float, y_mm: float) -> float:
    """Minimum plan-view distance from any segment to a point."""
    pt = ShapelyPoint(x_mm, y_mm)
    return min(s.distance(pt) for s in segs)


def vias_in_path(path) -> set:
    """Cells (c, r) where the grid path changes layer."""
    return {(a[0], a[1]) for a, b in zip(path, path[1:]) if a[2] != b[2]}


def overlap_area(model, deltas: dict, uuids: Tuple[str, str] = ("uuid-a", "uuid-b")) -> float:
    """Courtyard intersection area of two footprints at posed positions."""
    import numpy as np

    from kicad_autorouter.board_model import apply_deltas
    from kicad_autorouter.placement import (
        _collect_pad_nodes,
        _footprint_courtyard_polygon,
    )

    moved = apply_deltas(model, deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    polys = []
    for uuid in uuids:
        fp = next(f for f in moved.footprints if f.uuid == uuid)
        polys.append(_footprint_courtyard_polygon(
            fp, pad_positions=pos, pad_indices=uuid_to_idx.get(uuid, [])))
    if polys[0].is_empty or polys[1].is_empty:
        return 0.0
    return polys[0].intersection(polys[1]).area
