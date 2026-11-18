from __future__ import annotations
"""Shove eviction: boxes, clearance, restore (split from shove.py)."""
"""Local shove: evict-split-restore retry for walled nets.

Recovery layer (with ``retry``): sever-and-restore retries that run
*around* the engine core, never inside it — this module may import
``pipeline``, ``route_raw`` and ``output``, but nothing under
``routing.*`` may import it back. Callers enter through
``retry_failed_with_shove`` / ``retry_failed_with_signal_shove``
(usually via ``retry.route_with_retry``); outcomes ride ``ShoveReport``.

Sequential routing can wall a later net behind rail copper committed by
an earlier stage. Global eviction would destroy that committed mesh,
and reordering only moves the blockage (tube116: R28 / R31 / C2OutCV /
C2OutCVLED fail on a fresh grid with rails present, yet route once the
copper around their pins is lifted).

This module implements the scoped version: for one failed net, blocker
copper strictly inside per-pin boxes is cleared from scratch
model+tree copies (tracks are *split* at the box boundary so
outside-box copper — and its connectivity — survives untouched), the
net is routed alone, and cleared copper that still clears the new
copper is restored. Either the net routes with prior connectivity no
worse than before, or the net is fully rolled back and the board is
untouched.

GND shove (``retry_failed_with_shove``) evicts ground copper only,
tolerates partial restore under the GND pad-components fracture gate,
and bridges residual GND cuts with one gap flight before giving up
(same flight machinery as Tier-2, arbitrated by the same gate).
Signal shove (``retry_failed_with_signal_shove``) additionally severs
routed non-protected signal blockers and either restores every
*victim* piece bit-identical (Tier-1; GND leftovers stay under the
fracture gate) or reconnects severed victims across their gaps
(Tier-2 gap flights; coverage-gated) — else the attempt rolls back.
Power/rails/protected nets are never victims. True
mid-route push-and-shove (displacing live tracks under a violation cost
model) stays parked per docs/roadmap.md.
"""


import math
from dataclasses import dataclass
from dataclasses import replace as _replace
from typing import Collection, Dict, List, Optional, Sequence, Set, Tuple

from shapely.geometry import LineString, box as shapely_box
from shapely.ops import unary_union

from ..board_model import ArcTrack, BoardModel, Point, Track, Via, board_model
from ..board_model.model import (
    is_ground_net,
    is_power_net_effective,
)
from ..sexpr import SExpr
from .output import apply_routes_to_tree, routes_to_tracks_vias
from .pipeline import route_nets
from .stage_grid import _tht_sites_by_net
from .stage_flight import route_gap_terminals

# KiCad default board edge clearance (setup constraint) used as the
# acceptance gate for shoved copper when the tree carries no explicit
# value. The router's own edge keepout is a soft toll, so without this
# gate a detour can hug Edge.Cuts and stamp DRC-dirty copper.
DEFAULT_EDGE_CLEARANCE_MM = 0.5
# Split remnants below this are dropped (raster dust, not copper).
MIN_SPLIT_MM = 0.05




@dataclass
class ShoveReport:
    """Per-net outcome of a shove retry."""
    net: str
    routed: bool = False
    evicted: int = 0
    restored: int = 0
    reason: str = ""
    victims: Tuple[str, ...] = ()  # signal nets severed (signal shove only)
    gap_routed: bool = False  # a severed victim was reconnected by a gap flight


@dataclass
class _Middle:
    """One cleared inside-box piece of an evicted track (model + tree node)."""
    track: object = None  # model Track (middle piece)
    node: object = None  # tree (segment) node for the middle piece


@dataclass
class Eviction:
    """Scratch copies with inside-box GND cleared, plus restore ledger."""
    model: BoardModel
    tree: SExpr
    middles: Tuple[_Middle, ...] = ()
    removed_vias: Tuple = ()
    removed_arcs: Tuple = ()
    removed_via_nodes: Tuple = ()
    removed_arc_nodes: Tuple = ()
    evicted_count: int = 0


def pin_sites(model: BoardModel, net: str) -> List[Tuple[float, float]]:
    """Board-mm pin positions for a net."""
    return [(p.position.x_mm, p.position.y_mm)
            for fp in model.footprints for p in fp.pads
            if p.net_name == net]


def boxes_for_net(model: BoardModel, net: str,
                  radius_mm: float) -> Tuple[Tuple[float, float, float, float], ...]:
    """One square box per pin (radius ``radius_mm``), not the whole span.

    Whole-span boxes evict far more GND than the search needs (tube116
    R28: 117 tracks whole-span vs 59 per-pin); the search only needs its
    terminals unsealed plus maneuver room.
    """
    r = max(0.0, float(radius_mm))
    return tuple((x - r, y - r, x + r, y + r)
                 for x, y in pin_sites(model, net))


def _seg_hits_box(ax: float, ay: float, bx: float, by: float,
                  hit: Tuple[float, float, float, float]) -> bool:
    """True when segment AB touches the box (endpoints or edge cross)."""
    x0, y0, x1, y1 = hit
    if (x0 <= ax <= x1 and y0 <= ay <= y1) or (x0 <= bx <= x1 and y0 <= by <= y1):
        return True
    return LineString([(ax, ay), (bx, by)]).intersects(
        shapely_box(x0, y0, x1, y1))


def _split_pieces(ax: float, ay: float, bx: float, by: float,
                  hit: Tuple[float, float, float, float],
                  keep: str) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Sub-pieces of segment AB: ``keep`` is "outside" or "inside" the box.

    Splits (not amputation): outside pieces stay connected, inside
    pieces are the cleared middles. Remnants below MIN_SPLIT_MM drop.
    """
    import math
    x0, y0, x1, y1 = hit
    try:
        part = LineString([(ax, ay), (bx, by)])
        part = part.difference(shapely_box(x0, y0, x1, y1)) if keep == "outside" \
            else part.intersection(shapely_box(x0, y0, x1, y1))
    except Exception:
        return []
    if part.is_empty:
        return []
    out = []
    for g in list(getattr(part, "geoms", [part])):
        try:
            coords = list(g.coords)
        except Exception:
            continue
        if len(coords) < 2:
            continue
        (sx, sy), (ex, ey) = coords[0], coords[-1]
        if math.hypot(ex - sx, ey - sy) >= MIN_SPLIT_MM:
            out.append(((sx, sy), (ex, ey)))
    return out


def _node_xy(node: SExpr, key: str) -> Optional[Tuple[float, float]]:
    kid = node.find(key)
    if kid is None or len(kid.args) < 2:
        return None
    try:
        return (float(kid.args[0]), float(kid.args[1]))
    except (TypeError, ValueError):
        return None


def _node_net(node: SExpr) -> Optional[str]:
    n = node.find("net")
    if n is None or not n.args:
        return None
    a = n.args[0]
    return a if isinstance(a, str) else (a[0] if a else None)


def _seg_node(sx: float, sy: float, ex: float, ey: float,
              width: float, layer: str, net: Optional[str]) -> SExpr:
    return SExpr("segment", (
        SExpr("start", (round(sx, 3), round(sy, 3))),
        SExpr("end", (round(ex, 3), round(ey, 3))),
        SExpr("width", (width,)),
        SExpr("layer", (layer,)),
        SExpr("net", (net,)),
    ))


def evict_gnd_in_boxes(
        model: BoardModel, tree: SExpr,
        boxes: Sequence[Tuple[float, float, float, float]],
        victims: Collection[str] = ()) -> Eviction:
    """Scratch (model, tree) with GND strictly inside ``boxes`` cleared.

    ``victims`` additionally severs named signal nets inside the boxes
    (signal shove); ground copper always evicts. Tracks are split at
    box boundaries: outside pieces survive in place, inside middles go
    to the restore ledger. Vias/arcs intersecting a box are removed
    whole into the ledger (vias cannot split; arcs are rare enough that
    whole removal + restore is exact).
    """
    boxes = tuple(boxes)
    if not boxes:
        return Eviction(model=model, tree=tree)
    extra = set(victims or ())

    def _evictable(name: Optional[str]) -> bool:
        return bool(name) and (is_ground_net(name) or name in extra)

    def hit_box(ax, ay, bx, by):
        for b in boxes:
            if _seg_hits_box(ax, ay, bx, by, b):
                return b
        return None

    middles: List[_Middle] = []
    kept_tracks, kept_arcs, kept_vias = [], [], []
    removed_vias, removed_arcs = [], []
    for t in model.tracks:
        if not _evictable(t.net_name):
            kept_tracks.append(t)
            continue
        hit = hit_box(t.start.x_mm, t.start.y_mm, t.end.x_mm, t.end.y_mm)
        if hit is None:
            kept_tracks.append(t)
            continue
        for (sx, sy), (ex, ey) in _split_pieces(
                t.start.x_mm, t.start.y_mm, t.end.x_mm, t.end.y_mm, hit, "outside"):
            kept_tracks.append(Track(start=Point(sx, sy), end=Point(ex, ey),
                                     width_mm=t.width_mm, layer=t.layer,
                                     net_name=t.net_name))
        for (sx, sy), (ex, ey) in _split_pieces(
                t.start.x_mm, t.start.y_mm, t.end.x_mm, t.end.y_mm, hit, "inside"):
            middles.append(_Middle(track=Track(
                start=Point(sx, sy), end=Point(ex, ey),
                width_mm=t.width_mm, layer=t.layer, net_name=t.net_name),
                node=None))
    for a in model.arcs:
        if not _evictable(a.net_name):
            kept_arcs.append(a)
            continue
        pts = [(a.start.x_mm, a.start.y_mm), (a.mid.x_mm, a.mid.y_mm),
               (a.end.x_mm, a.end.y_mm)]
        if not any(hit_box(px, py, qx, qy) is not None
                   for (px, py), (qx, qy) in zip(pts, pts[1:])):
            kept_arcs.append(a)
            continue
        removed_arcs.append(a)
    for v in model.vias:
        if not _evictable(v.net_name):
            kept_vias.append(v)
            continue
        if any(b[0] <= v.position.x_mm <= b[2] and b[1] <= v.position.y_mm <= b[3]
               for b in boxes):
            removed_vias.append(v)
        else:
            kept_vias.append(v)
    model2 = _replace(model, tracks=tuple(kept_tracks), arcs=tuple(kept_arcs),
                      vias=tuple(kept_vias))

    kept_nodes, removed_arc_nodes, removed_via_nodes = [], [], []
    mid_nodes: List[SExpr] = []
    for c in tree.args:
        if c.head not in ("segment", "arc", "via") or not _evictable(_node_net(c)):
            kept_nodes.append(c)
            continue
        if c.head == "via":
            at = _node_xy(c, "at")
            if at is not None and any(
                    b[0] <= at[0] <= b[2] and b[1] <= at[1] <= b[3] for b in boxes):
                removed_via_nodes.append(c)
            else:
                kept_nodes.append(c)
            continue
        if c.head == "arc":
            s, m, e = _node_xy(c, "start"), _node_xy(c, "mid"), _node_xy(c, "end")
            pts = [p for p in (s, m, e) if p is not None]
            if len(pts) >= 2 and any(hit_box(px, py, qx, qy) is not None
                                     for (px, py), (qx, qy) in zip(pts, pts[1:])):
                removed_arc_nodes.append(c)
            else:
                kept_nodes.append(c)
            continue
        s, e = _node_xy(c, "start"), _node_xy(c, "end")
        if s is None or e is None:
            kept_nodes.append(c)
            continue
        hit = hit_box(s[0], s[1], e[0], e[1])
        if hit is None:
            kept_nodes.append(c)
            continue
        width = c.find("width")
        layer = c.find("layer")
        netn = c.find("net")
        w = float(width.args[0]) if width is not None else 0.5
        lay = layer.args[0] if layer is not None and layer.args else "B.Cu"
        nn = _node_net(c)
        for (sx, sy), (ex, ey) in _split_pieces(s[0], s[1], e[0], e[1], hit, "outside"):
            kept_nodes.append(_seg_node(sx, sy, ex, ey, w, lay, nn))
        for (sx, sy), (ex, ey) in _split_pieces(s[0], s[1], e[0], e[1], hit, "inside"):
            mid_nodes.append(_seg_node(sx, sy, ex, ey, w, lay, nn))
    tree2 = _replace(tree, args=tuple(kept_nodes))
    # Pair middle tree nodes with middle model tracks by rounded
    # geometry plus layer (model order is not a contract for tree
    # order; same-XY walls on both layers must not share a node —
    # a stolen node restores the wrong layer's copper and double-adds
    # one ledger node while dropping the other).
    def _key(track):
        return (round(track.start.x_mm, 3), round(track.start.y_mm, 3),
                round(track.end.x_mm, 3), round(track.end.y_mm, 3),
                track.layer, track.net_name)
    node_by_key: Dict[tuple, SExpr] = {}
    for n in mid_nodes:
        s, e = _node_xy(n, "start"), _node_xy(n, "end")
        if s is not None and e is not None:
            lay = n.find("layer")
            lname = lay.args[0] if lay is not None and lay.args else ""
            node_by_key.setdefault(
                (round(s[0], 3), round(s[1], 3),
                 round(e[0], 3), round(e[1], 3), lname,
                 _node_net(n)), n)
    paired = tuple(_Middle(track=m.track, node=node_by_key.get(_key(m.track)))
                   for m in middles)
    return Eviction(
        model=model2, tree=tree2, middles=paired,
        removed_vias=tuple(removed_vias), removed_arcs=tuple(removed_arcs),
        removed_via_nodes=tuple(removed_via_nodes),
        removed_arc_nodes=tuple(removed_arc_nodes),
        evicted_count=len(paired) + len(removed_vias) + len(removed_arcs),
    )


def gnd_pad_components(model: BoardModel) -> int:
    """GND pad-components (buffered-copper union-find, KiCad-DRC-like).

    Counts components touching a GND pad. Compare before/after shove:
    must not increase (no new fractures).
    """
    pad_polys = []
    for fp in model.footprints:
        for p in fp.pads:
            if is_ground_net(p.net_name):
                hx, hy = p.size_mm[0] / 2.0, p.size_mm[1] / 2.0
                x, y = p.position.x_mm, p.position.y_mm
                pad_polys.append(shapely_box(x - hx, y - hy, x + hx, y + hy))
    lines = []
    for t in list(model.tracks) + list(model.arcs):
        if is_ground_net(t.net_name):
            pts = [(t.start.x_mm, t.start.y_mm), (t.end.x_mm, t.end.y_mm)]
            try:
                pts.insert(1, (t.mid.x_mm, t.mid.y_mm))
            except AttributeError:
                pass
            lines.append(LineString(pts))
    for v in model.vias:
        if is_ground_net(v.net_name):
            x, y = v.position.x_mm, v.position.y_mm
            pad_polys.append(shapely_box(x - 1e-6, y - 1e-6, x + 1e-6, y + 1e-6))
    if not pad_polys and not lines:
        return 0
    copper = unary_union(
        [g.buffer(0.001, cap_style="flat") for g in lines] + pad_polys)
    parts = list(getattr(copper, "geoms", [copper]))
    touched = set()
    for pi, poly in enumerate(parts):
        for pp in pad_polys:
            if poly.intersects(pp):
                touched.add(pi)
                break
    return len(touched)


def new_copper_nodes(before: SExpr, after: SExpr) -> List[SExpr]:
    """Copper nodes appended by a route (identity diff, order-proof)."""
    pre = {id(c) for c in before.args}
    return [c for c in after.args
            if id(c) not in pre and c.head in ("segment", "arc", "via")]


def _clearance_of(node: SExpr) -> Tuple[float, str]:
    """(half-width radius, layers-text) for clearance checks of new copper."""
    half = 0.125
    if node.head == "via":
        size = node.find("size")
        try:
            half = float(size.args[0]) / 2.0 if size is not None else 0.4
        except (TypeError, ValueError, IndexError):
            half = 0.4
        layers = node.find("layers")
        lname = " ".join(str(a) for a in layers.args) if layers is not None else ""
        return half, lname if isinstance(lname, str) else ""
    w = node.find("width")
    try:
        half = float(w.args[0]) / 2.0 if w is not None else 0.125
    except (TypeError, ValueError, IndexError):
        half = 0.125
    layer = node.find("layer")
    lname = layer.args[0] if layer is not None and layer.args else ""
    return half, lname if isinstance(lname, str) else ""


def _new_copper_clearance(new_nodes: List[SExpr], clearance_mm: float):
    """Clearance predicate of routed copper: ``ok(g, halfwidth, layer,
    same_layer_only)`` is True when geometry ``g`` keeps ``clearance_mm``
    from every new node (same-layer segments only when asked; vias
    check all new copper since the barrel spans layers).
    """
    from shapely.geometry import Point as SPoint

    new_segs = []  # (geometry, halfwidth, layers-text)
    for n in new_nodes:
        half, layers = _clearance_of(n)
        if n.head == "via":
            at = _node_xy(n, "at")
            if at is not None:
                new_segs.append((SPoint(at), half, layers))
        else:
            s, e = _node_xy(n, "start"), _node_xy(n, "end")
            m = _node_xy(n, "mid")
            pts = [s] + ([m] if m else []) + [e] if s and e else []
            if len(pts) >= 2:
                new_segs.append((LineString(pts), half, layers))

    def seg_ok(g, halfwidth: float, layer: str, same_layer_only: bool) -> bool:
        for ng, nhalf, nlayers in new_segs:
            if same_layer_only and layer not in nlayers:
                continue
            try:
                gap = g.distance(ng) - (halfwidth + nhalf)
            except Exception:
                return False
            if gap < clearance_mm:
                return False
        return True

    return seg_ok


def _restore_cleared(model: BoardModel, tree: SExpr, ev: Eviction,
                     new_nodes: List[SExpr],
                     clearance_mm: float
                     ) -> Tuple[BoardModel, SExpr, int, Tuple]:
    """Re-add cleared copper that still clears the new copper.

    Middle track pieces check same-layer segments only (different Cu
    layers never clearance-conflict); vias check all new copper (the
    barrel spans layers, so a new via at the same XY would short).
    Arcs chord-check same-layer. Returns (model, tree, restored count,
    kept middles) — the kept ledger middles let gap flights limit
    search terminals to genuinely unrestored cuts.
    """
    from shapely.geometry import Point as SPoint

    seg_ok = _new_copper_clearance(new_nodes, clearance_mm)

    # Only middles with a paired ledger node restore (model and tree
    # must never disagree; an unpaired middle stays evicted).
    keep_mid = [m for m in ev.middles
                if m.node is not None and seg_ok(
                    LineString([(m.track.start.x_mm, m.track.start.y_mm),
                                (m.track.end.x_mm, m.track.end.y_mm)]),
                    m.track.width_mm / 2.0, m.track.layer, True)]
    keep_arcs = []
    for a in ev.removed_arcs:
        try:
            g = LineString([(a.start.x_mm, a.start.y_mm),
                            (a.mid.x_mm, a.mid.y_mm),
                            (a.end.x_mm, a.end.y_mm)])
        except Exception:
            continue
        if seg_ok(g, a.width_mm / 2.0, a.layer, True):
            keep_arcs.append(a)
    keep_vias = [v for v in ev.removed_vias
                 if seg_ok(SPoint(v.position.x_mm, v.position.y_mm),
                           v.size_mm / 2.0, "", False)]

    model2 = _replace(
        model,
        tracks=model.tracks + tuple(m.track for m in keep_mid),
        arcs=model.arcs + tuple(keep_arcs),
        vias=model.vias + tuple(keep_vias),
    )
    # Middle pieces reuse their exact ledger nodes (uuids preserved).
    restored_nodes = [m.node for m in keep_mid if m.node is not None]
    # Whole-removed arcs/vias restore 1:1 from the ledger, matched to
    # the kept model pieces by rounded geometry (parse order is not a
    # contract, coordinates are).
    keep_arc_keys = {
        (round(a.start.x_mm, 3), round(a.start.y_mm, 3),
         round(a.mid.x_mm, 3), round(a.mid.y_mm, 3),
         round(a.end.x_mm, 3), round(a.end.y_mm, 3),
         a.layer, a.net_name) for a in keep_arcs}
    for n in ev.removed_arc_nodes:
        s, m, e = _node_xy(n, "start"), _node_xy(n, "mid"), _node_xy(n, "end")
        lay = n.find("layer")
        lname = lay.args[0] if lay is not None and lay.args else ""
        if s is not None and m is not None and e is not None and (
                round(s[0], 3), round(s[1], 3), round(m[0], 3),
                round(m[1], 3), round(e[0], 3), round(e[1], 3),
                lname, _node_net(n)) in keep_arc_keys:
            restored_nodes.append(n)
    keep_via_keys = {(round(v.position.x_mm, 3), round(v.position.y_mm, 3),
                      v.net_name)
                     for v in keep_vias}
    for n in ev.removed_via_nodes:
        at = _node_xy(n, "at")
        if at is not None and (round(at[0], 3), round(at[1], 3),
                               _node_net(n)) in keep_via_keys:
            restored_nodes.append(n)
    tree2 = _replace(tree, args=tuple(tree.args) + tuple(restored_nodes))
    return (model2, tree2, len(keep_mid) + len(keep_arcs) + len(keep_vias),
            tuple(keep_mid))


def victim_pieces_intact(ev: Eviction, new_nodes: List[SExpr],
                          victims: Collection[str],
                          clearance_mm: float) -> bool:
    """True when every evicted piece of ``victims`` still clears the new copper.

    Tier-1 signal-shove gate: severed victims must restore bit-identical,
    so each ledger piece is re-checked against the routed copper with
    the same geometry the restore pass uses. Unpaired middles can never
    restore (model and tree must agree), so they fail the gate.
    """
    from shapely.geometry import Point as SPoint

    victim_set = set(victims or ())
    if not victim_set:
        return True
    seg_ok = _new_copper_clearance(new_nodes, clearance_mm)
    for m in ev.middles:
        if m.track.net_name not in victim_set:
            continue
        if m.node is None:
            return False
        if not seg_ok(
                LineString([(m.track.start.x_mm, m.track.start.y_mm),
                            (m.track.end.x_mm, m.track.end.y_mm)]),
                m.track.width_mm / 2.0, m.track.layer, True):
            return False
    for a in ev.removed_arcs:
        if a.net_name not in victim_set:
            continue
        try:
            g = LineString([(a.start.x_mm, a.start.y_mm),
                            (a.mid.x_mm, a.mid.y_mm),
                            (a.end.x_mm, a.end.y_mm)])
        except Exception:
            return False
        if not seg_ok(g, a.width_mm / 2.0, a.layer, True):
            return False
    for v in ev.removed_vias:
        if v.net_name not in victim_set:
            continue
        if not seg_ok(SPoint(v.position.x_mm, v.position.y_mm),
                       v.size_mm / 2.0, "", False):
            return False
    return True
