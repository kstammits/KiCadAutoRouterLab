"""Shared builders for routing/placement tests.

Shared ``Pad(...)`` spells, courtyard builders, two-pad footprint factories,
and plan-view geometry helpers used across the routing and placement tests.

Convention (matches all prior call sites): pads are built at the origin
and placed with ``dataclasses.replace(pad, position=...)``; footprints
take board-space ``(x, y)`` with pads at ``±pad_dx``. Placement-style
origin-centered courtyards use ``courtyard_origin=True``.
"""

from __future__ import annotations

from dataclasses import replace  # noqa: F401  (re-exported for call sites)
from pathlib import Path
from typing import Optional, Sequence, Tuple

from shapely.geometry import LineString
from shapely.geometry import Point as ShapelyPoint

from kicad_autorouter.board_model import (
    BoardModel,
    Footprint,
    NetConnection,
    Pad,
    Point,
    SilkLabel,
    arcs,
    sample_circular_arc,
    tracks,
)

FIXTURES = Path(__file__).parent / "fixtures"


def post_fixture(base: str, fixture: str = "minimal.kicad_pcb",
                 name: Optional[str] = None, data: Optional[bytes] = None) -> dict:
    """POST a fixture board to a live test server; return the parsed JSON."""
    import json
    import urllib.request

    body = data if data is not None else (FIXTURES / fixture).read_bytes()
    req = urllib.request.Request(
        f"{base}/api/load?name={name or Path(fixture).stem}",
        data=body, method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def stamp_edge_cuts(tree, half: float = 25.0):
    """Rewrite Edge.Cuts rectangles to the test board square.

    Synthetic trees start from minimal.kicad_pcb, whose outline lives
    far from small-coordinate test boards. Engine stages that reparse
    the tree (retry/shove) build grids from those cuts, so a mismatch
    collapses every terminal onto one clamped cell and routing
    "succeeds" vacuously. Stamp after adding footprints/tracks.
    """
    from kicad_autorouter.sexpr import SExpr

    new_args = []
    for c in tree.args:
        if c.head == "gr_rect" and any(
                k.head == "layer" and "Edge.Cuts" in k.args for k in c.args):
            kids = []
            for k in c.args:
                if k.head == "start":
                    kids.append(SExpr("start", (-half, -half)))
                elif k.head == "end":
                    kids.append(SExpr("end", (half, half)))
                else:
                    kids.append(k)
            new_args.append(SExpr(c.head, tuple(kids)))
        else:
            new_args.append(c)
    return SExpr(tree.head, tuple(new_args))


def make_board(fps, edge: Optional[float] = 30.0, regions=None) -> BoardModel:
    """Square-edge-cuts board; every footprint in region 0.

    ``edge=None`` means no edge cuts; ``regions`` overrides the empty
    ``board_regions`` (movement-style region tests).
    """
    fps = tuple(fps)
    cuts = (
        (Point(-edge, -edge), Point(edge, -edge)),
        (Point(edge, -edge), Point(edge, edge)),
        (Point(edge, edge), Point(-edge, edge)),
        (Point(-edge, edge), Point(-edge, -edge)),
    ) if edge is not None else ()
    return BoardModel(
        footprints=fps,
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=cuts,
        board_regions=() if regions is None else tuple(regions),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


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
    """Plan-view LineStrings of a net's tracks on one copper layer.

    (arc) copper is densely sampled along the true circular arc, not
    approximated as start→mid→end chords: long sweep arcs bow tens of
    mm off their chords, so chord approximation misplaces the copper
    in plan view (missed crossings, phantom keepout entries).
    Sampling every ~0.5mm matches the engine's own arc treatment.
    """
    segs = []
    for tr in tracks(pcb_tree):
        if tr.net_name == net and tr.layer == layer:
            segs.append(LineString([
                (tr.start.x_mm, tr.start.y_mm),
                (tr.end.x_mm, tr.end.y_mm),
            ]))
    for ar in arcs(pcb_tree):
        if ar.net_name == net and ar.layer == layer:
            start = (ar.start.x_mm, ar.start.y_mm)
            mid = (ar.mid.x_mm, ar.mid.y_mm)
            end = (ar.end.x_mm, ar.end.y_mm)
            chord = LineString([start, end]).length
            n = max(16, int(chord / 0.5) + 1)
            pts = list(sample_circular_arc(start, mid, end, n))
            segs.append(LineString(pts))
    return segs


def closest_approach(segs: list, x_mm: float, y_mm: float) -> float:
    """Minimum plan-view distance from any segment to a point."""
    pt = ShapelyPoint(x_mm, y_mm)
    return min(s.distance(pt) for s in segs)


def vias_in_path(path) -> set:
    """Cells (c, r) where the grid path changes layer."""
    return {(a[0], a[1]) for a, b in zip(path, path[1:]) if a[2] != b[2]}


def zeros_grid(size=30, layers=1):
    """Zeroed 3D cost grid ``(layers, size, size)`` (smoothing suites)."""
    import numpy as np

    return np.zeros((layers, size, size), dtype=np.int16)


def zeros_plane(n=40):
    """Zeroed 2D cost plane ``(n, n)`` (biarc suites)."""
    import numpy as np

    return np.zeros((n, n), dtype=np.int16)


def ladder_board(n_pads: int, net: str = "BIG",
                 uuid_prefix: str = "uuid-big") -> BoardModel:
    """One single-pad footprint per terminal, spread along x.

    Shared by the chunking/limits suites (previously duplicated
    ``_ladder`` builders differing only in the uuid prefix).
    """
    fps = [
        make_fp(f"U{i}", f"{uuid_prefix}-{i:03d}", x=float(i) * 4.0,
                nets=(net,))
        for i in range(n_pads)
    ]
    extent = n_pads * 4.0 + 10.0
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets={net: tuple(NetConnection(ref=f"U{i}", pad_number="1")
                         for i in range(n_pads))},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(0.0, 0.0), Point(extent, 0.0)),
            (Point(extent, 0.0), Point(extent, extent)),
            (Point(extent, extent), Point(0.0, extent)),
            (Point(0.0, extent), Point(0.0, 0.0)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


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


def facing_label_pair(label_w_mm: float = 6.0, gap_mm: float = 4.0):
    """Two netted parts side by side with big facing labels.

    Courtyards (hw=1.5) start ``gap_mm`` apart center-to-center: inside
    the 6mm wall halo so the pair feels contact; the labels sit on the
    facing edges to force label-body overlap only when solidity unions
    them in. Shared by the label placement/orbit suites.
    """
    def _lab(ref, lx):
        return SilkLabel(
            text="REFDES",
            local_x_mm=lx,
            local_y_mm=0.0,
            local_angle_deg=0.0,
            layer="F.SilkS",
            size_mm=(label_w_mm / (6 * 0.6), 1.0),
            thickness_mm=0.15,
            uuid=f"{ref}-label",
        )

    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 2.0),
    )
    fb = replace(
        make_fp("B", "uuid-b", gap_mm, 0.0, ("N1", "N3"), hw=1.5, hh=1.0),
        label=_lab("B", -2.0),
    )
    return replace(make_board([fa, fb]), nets={"N1": (), "N2": (), "N3": ()})
