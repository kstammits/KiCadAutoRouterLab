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

from __future__ import annotations

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
from .route_raw import _tht_sites_by_net, route_gap_terminals

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


def _on_box_edge(x: float, y: float, boxes, eps: float = 1e-6) -> bool:
    """True when a point lies on any box boundary (cut joint candidate)."""
    for (x0, y0, x1, y1) in boxes:
        on_v = (min(abs(x - x0), abs(x - x1)) < eps
                and y0 - eps <= y <= y1 + eps)
        on_h = (min(abs(y - y0), abs(y - y1)) < eps
                and x0 - eps <= x <= x1 + eps)
        if on_v or on_h:
            return True
    return False


def _cut_points_for(ev: Eviction, boxes, victim: str,
                     eps: float = 1e-6) -> Tuple[Tuple[float, float, str], ...]:
    """Box-boundary endpoints of severed ``victim`` middles (gap terminals).

    A middle endpoint lying on a box edge is where kept copper was cut;
    interior endpoints are dead ends needing no join. Rounded for
    dedupe; the middle's layer rides along for grid mapping.
    """
    cuts = []
    for m in ev.middles:
        if m.track.net_name != victim:
            continue
        for (x, y) in ((m.track.start.x_mm, m.track.start.y_mm),
                       (m.track.end.x_mm, m.track.end.y_mm)):
            if _on_box_edge(x, y, boxes, eps):
                cuts.append((round(x, 3), round(y, 3), m.track.layer))
    return tuple(dict.fromkeys(cuts))


def victim_net_components(model: BoardModel, net: str) -> int:
    """Pad components for one signal net (layer-aware gap coverage).

    Union-find over the victim's copper pieces, joining only on shared
    layers: an SMD pad is blind to opposite-layer copper (unlike the
    layer-blind GND check — a restored B.Cu middle does not reconnect
    F.Cu SMD pads); THT barrels and vias span their layer sets. Pieces
    buffer by true halfwidth, so a gap flight landing within a sliver
    of the stub end counts as joined (cell-center emission stops short
    of the exact cut); genuinely open gaps stay split. Returns the
    number of distinct components touching a pad — 1 means every pad
    joins one connected island (kept + restored + gap copper all
    count). Floating copper without pads is ignored.
    """
    from shapely.geometry import Point as SPoint

    geoms = []  # (geometry, frozenset(layers), is_pad)
    for fp in model.footprints:
        for p in fp.pads:
            if p.net_name != net:
                continue
            hx, hy = p.size_mm[0] / 2.0, p.size_mm[1] / 2.0
            x, y = p.position.x_mm, p.position.y_mm
            geoms.append((shapely_box(x - hx, y - hy, x + hx, y + hy),
                          frozenset(p.layers or ()), True))
    for t in list(model.tracks) + list(model.arcs):
        if t.net_name != net:
            continue
        pts = [(t.start.x_mm, t.start.y_mm), (t.end.x_mm, t.end.y_mm)]
        try:
            pts.insert(1, (t.mid.x_mm, t.mid.y_mm))
        except AttributeError:
            pass
        geoms.append((LineString(pts).buffer(t.width_mm / 2.0 + 1e-6),
                      frozenset((t.layer,)), False))
    for v in model.vias:
        if v.net_name != net:
            continue
        x, y = v.position.x_mm, v.position.y_mm
        geoms.append((SPoint(x, y).buffer(v.size_mm / 2.0 + 1e-6),
                      frozenset(v.layers or ()), False))
    parent = list(range(len(geoms)))

    def find(a):
        # DSU find with path halving (local to via clustering).
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(len(geoms)):
        gi, li, _ = geoms[i]
        for j in range(i + 1, len(geoms)):
            gj, lj, _ = geoms[j]
            if not (li & lj):
                continue
            try:
                touch = gi.intersects(gj)
            except Exception:
                continue
            if touch:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    return len({find(i) for i, (_, _, is_pad) in enumerate(geoms) if is_pad})


def _isolated_victim_pads(model: BoardModel, victim: str
                     ) -> Tuple[Tuple[float, float, str], ...]:
    """Victim pads touching no kept victim copper (gap terminals).

    Layer-aware true-copper touch (centerlines buffered by halfwidth):
    an SMD pad needs same-layer copper; THT barrels and vias span
    their layer sets. Pads already on kept stubs are excluded from the
    search terminal set — a flight ending on the nearest pad would join
    it without ever crossing the gap — but they still join coverage as
    already-connected cells.
    """
    out = []
    for fp in model.footprints:
        for p in fp.pads:
            if p.net_name != victim:
                continue
            hx, hy = p.size_mm[0] / 2.0, p.size_mm[1] / 2.0
            x, y = p.position.x_mm, p.position.y_mm
            rect = shapely_box(x - hx, y - hy, x + hx, y + hy)
            layers = set(p.layers or ())
            touched = False
            for t in list(model.tracks) + list(model.arcs):
                if t.net_name != victim or t.layer not in layers:
                    continue
                pts = [(t.start.x_mm, t.start.y_mm), (t.end.x_mm, t.end.y_mm)]
                try:
                    pts.insert(1, (t.mid.x_mm, t.mid.y_mm))
                except AttributeError:
                    pass
                try:
                    half = t.width_mm / 2.0
                    if LineString(pts).buffer(half + 1e-6).intersects(rect):
                        touched = True
                        break
                except Exception:
                    continue
            if not touched:
                for v in model.vias:
                    if v.net_name != victim or not (layers & set(v.layers or ())):
                        continue
                    from shapely.geometry import Point as SPoint
                    try:
                        if SPoint(v.position.x_mm, v.position.y_mm).buffer(
                                v.size_mm / 2.0 + 1e-6).intersects(rect):
                            touched = True
                            break
                    except Exception:
                        continue
            if not touched:
                out.append((x, y, fp.layer))
    return tuple(out)


def _snap_emit_ends(tracks, terms, tol_mm: float) -> None:
    """Snap emitted bridge ends exactly onto their search terminals.

    Cell-center emission stops short of the severed cut (up to half a
    grid diagonal away); the precise model-space gate reads that sliver
    as a split. Endpoints already sit in their terminal's cell, so
    moving them onto the cut stays inside grid-clearance-inflated free
    space. Mutates the output-side tracks in place.
    """
    for t in tracks:
        for attr in ("start", "end"):
            p = getattr(t, attr, None)
            if p is None:
                continue
            for (cx, cy, clayer) in terms:
                if clayer != t.layer:
                    continue
                try:
                    gap = math.hypot(p[0] - cx, p[1] - cy)
                except (TypeError, IndexError):
                    continue
                if gap <= tol_mm:
                    setattr(t, attr, (round(cx, 3), round(cy, 3)))
                    break


def _fly_one_gap(
        model: BoardModel, tree: SExpr, ev: Eviction, victim: str,
        terms: Sequence[Tuple[float, float, str]], params,
        clearance_mm: float, edge_mm: float,
        extra_power: Collection[str] = (),
        snap_tol_mm: float = 0.0,
        skip_coverage: bool = False,
        progress_cb=None, should_cancel=None,
) -> Tuple[bool, BoardModel, SExpr, str]:
    """One gap-reconnect flight plus emit for a severed net (Tier-2 core).

    Flies ``route_gap_terminals`` over the given search terminals with
    kept copper as walkable seed and evicted copper punched back to
    free, emits, and checks seeded coverage plus edge clearance.
    ``snap_tol_mm`` > 0 snaps emitted ends onto same-layer terminals
    first (exact joints for precise model-space gates).
    ``skip_coverage`` ignores the seeded-coverage verdict and lets the
    caller's model-space gate decide alone (for nets whose pads never
    form one island — GND tolerates pre-existing islands, so grid
    coverage over all its pads is the wrong bar). Returns ``(ok,
    model, tree, reason)``; any failure rolls the whole attempt back
    (eviction was on copies, so rollback is free).
    """
    seed_tracks = [
        (t.start.x_mm, t.start.y_mm, t.end.x_mm, t.end.y_mm, t.layer)
        for t in model.tracks if t.net_name == victim]
    seed_tracks += [
        (a.start.x_mm, a.start.y_mm, a.mid.x_mm, a.mid.y_mm, a.layer)
        for a in model.arcs if a.net_name == victim] + [
        (a.mid.x_mm, a.mid.y_mm, a.end.x_mm, a.end.y_mm, a.layer)
        for a in model.arcs if a.net_name == victim]
    seed_vias = [(v.position.x_mm, v.position.y_mm)
                 for v in model.vias if v.net_name == victim]
    punch_tracks = [
        (m.track.start.x_mm, m.track.start.y_mm,
         m.track.end.x_mm, m.track.end.y_mm, m.track.layer)
        for m in ev.middles]
    punch_tracks += [
        (a.start.x_mm, a.start.y_mm, a.mid.x_mm, a.mid.y_mm, a.layer)
        for a in ev.removed_arcs] + [
        (a.mid.x_mm, a.mid.y_mm, a.end.x_mm, a.end.y_mm, a.layer)
        for a in ev.removed_arcs]
    punch_vias = [(v.position.x_mm, v.position.y_mm)
                  for v in ev.removed_vias]
    result, reason, grid, width, coverage = route_gap_terminals(
        model, victim, terms, params=params,
        seed_tracks=seed_tracks, seed_vias=seed_vias,
        punch_tracks=punch_tracks, punch_vias=punch_vias,
        extra_power_nets=tuple(extra_power or ()),
        progress_cb=progress_cb, should_cancel=should_cancel)
    if result is None:
        return False, model, tree, (
            f"gap reconnect failed for {victim}: {reason}")
    tracks, vias, arcs = routes_to_tracks_vias(
        {victim: result}, grid, net_class_widths={victim: width},
        default_width_mm=params.track_width_mm,
        power_width_mm=params.power_width_mm,
        via_size_mm=params.via_size_mm, via_drill_mm=params.via_drill_mm,
        tht_sites=_tht_sites_by_net(model, grid),
        extra_power=tuple(extra_power or ()))
    if snap_tol_mm > 0 and terms:
        _snap_emit_ends(tracks, terms, snap_tol_mm)
    model = _replace(
        model,
        tracks=model.tracks + tuple(
            Track(start=Point(*t.start), end=Point(*t.end),
                  width_mm=t.width_mm, layer=t.layer,
                  net_name=t.net_name)
            for t in tracks),
        arcs=model.arcs + tuple(
            ArcTrack(start=Point(*a.start), mid=Point(*a.mid),
                     end=Point(*a.end), width_mm=a.width_mm,
                     layer=a.layer, net_name=a.net_name)
            for a in arcs),
        vias=model.vias + tuple(
            Via(position=Point(*v.position), size_mm=v.size_mm,
                drill_mm=v.drill_mm, layers=tuple(v.layers),
                net_name=v.net_name)
            for v in vias),
    )
    before = tree
    tree = apply_routes_to_tree(tree, tracks, vias, arcs)
    if not edge_clearance_ok(new_copper_nodes(before, tree),
                             model.edge_cuts, edge_mm):
        return False, model, tree, "gap edge-clearance rollback"
    if coverage is not None and not skip_coverage:
        return False, model, tree, (
            f"gap reconnect incomplete for {victim}: {coverage}")
    return True, model, tree, ""


def _reconnect_victim_gaps(
        model: BoardModel, tree: SExpr, ev: Eviction,
        boxes, victims: Collection[str], params,
        clearance_mm: float, edge_mm: float,
        extra_power: Collection[str] = (),
        progress_cb=None, should_cancel=None,
) -> Tuple[bool, BoardModel, SExpr, str]:
    """Tier-2: route each severed victim back together across its gaps.

    Runs on the restored scratch (kept + restored copper committed):
    victims already whole are skipped; others get one
    ``route_gap_terminals`` flight each (cut points + isolated pads as
    search terminals — connected pads are excluded so the flight must
    cross the gap — kept stubs as walkable seed), emitted and checked
    for seeded coverage and edge clearance. Returns ``(ok, model,
    tree, reason)``; any failure rolls the whole attempt back
    (eviction was on copies, so rollback is free).
    """
    for victim in victims:
        if victim_net_components(model, victim) <= 1:
            continue
        terms = list(_cut_points_for(ev, boxes, victim))
        terms += list(_isolated_victim_pads(model, victim))
        if len(terms) < 2:
            return False, model, tree, (
                f"gap reconnect failed for {victim}: "
                f"gap: fewer than 2 terminals")
        ok, model, tree, why = _fly_one_gap(
            model, tree, ev, victim, terms, params, clearance_mm,
            edge_mm, extra_power, snap_tol_mm=0.0, skip_coverage=False,
            progress_cb=progress_cb, should_cancel=should_cancel)
        if not ok:
            return False, model, tree, why
        if victim_net_components(model, victim) != 1:
            return False, model, tree, (
                f"gap reconnect incomplete for {victim}: "
                f"model copper still split")
    return True, model, tree, ""


def _reconnect_gnd_gap(
        model: BoardModel, tree: SExpr, ev: Eviction, boxes,
        kept_middles: Collection, params,
        clearance_mm: float, edge_mm: float, base_gnd: int,
        progress_cb=None, should_cancel=None,
) -> Tuple[bool, BoardModel, SExpr, str]:
    """Tier-2 for GND leftovers: bridge residual GND cuts across gaps.

    Same flight machinery as victim reconnect, but the arbiter is the
    layer-blind GND fracture gate: only unrestored middles supply cut
    terminals (restored copper rejoins exactly, so its cuts need no
    flight), removed vias supply their barrel sites (a severed barrel
    joins on any of its layers), isolated GND pads join only from
    inside the shove boxes (outside-box pads still touch kept copper —
    eviction never reaches them), and emitted bridge ends snap exactly
    onto their cuts. Seeded coverage is skipped: GND tolerates
    pre-existing islands, so coverage over all its pads can never pass
    — the fracture gate decides. The bridge width resolves as ordinary
    GND copper. Returns ``(ok, model, tree, reason)``; failure rolls
    the whole attempt back.
    """
    if gnd_pad_components(model) <= base_gnd:
        return True, model, tree, ""
    skip = {id(k) for k in kept_middles or ()}
    unrestored = [m for m in ev.middles if id(m) not in skip]
    cuts = _cut_points_for(
        _replace(ev, middles=tuple(unrestored)), boxes, "GND")
    terms = list(cuts)
    # Interior gap ends: middle endpoints off the box edges are where a
    # severed chain stops mid-box (its neighbor is another unrestored
    # piece or a removed barrel). They are the true gap ends — without
    # them every terminal trivially pairs with its same-XY partner and
    # the flight re-lays nothing.
    inner = []
    for m in unrestored:
        if m.track.net_name != "GND":
            continue
        for (x, y) in ((m.track.start.x_mm, m.track.start.y_mm),
                       (m.track.end.x_mm, m.track.end.y_mm)):
            if not _on_box_edge(x, y, boxes):
                inner.append((round(x, 3), round(y, 3), m.track.layer))
    terms += [p for p in dict.fromkeys(inner) if p not in set(terms)]
    for v in ev.removed_vias:
        for layer in v.layers or ():
            terms.append((v.position.x_mm, v.position.y_mm, layer))
    for (x, y, layer) in _isolated_victim_pads(model, "GND"):
        if any(b[0] - 1.0 <= x <= b[2] + 1.0 and
               b[1] - 1.0 <= y <= b[3] + 1.0 for b in boxes):
            terms.append((x, y, layer))
    if len(terms) < 2:
        return False, model, tree, "gap: fewer than 2 GND terminals"
    tol_mm = float(params.grid_resolution_mm) * 0.71 + 1e-3
    ok, model, tree, why = _fly_one_gap(
        model, tree, ev, "GND", terms, params, clearance_mm, edge_mm,
        extra_power=(), snap_tol_mm=tol_mm, skip_coverage=True,
        progress_cb=progress_cb, should_cancel=should_cancel)
    if not ok:
        return False, model, tree, why
    if gnd_pad_components(model) > base_gnd:
        return False, model, tree, (
            "gap reconnect incomplete for GND: model copper still split")
    return True, model, tree, ""


def edge_clearance_ok(new_nodes: List[SExpr], edge_cuts,
                       min_mm: float) -> bool:
    """New copper keeps board edge clearance (shove detours hug Edge.Cuts)."""
    if min_mm <= 0 or not edge_cuts:
        return True
    def _ends(s):
        # Segment = (Point, Point); tolerate objects with .start/.end.
        a = s.start if hasattr(s, "start") else s[0]
        b = s.end if hasattr(s, "end") else s[1]
        return [(a.x_mm, a.y_mm), (b.x_mm, b.y_mm)]
    try:
        edge = unary_union([LineString(_ends(s)) for s in edge_cuts])
    except Exception:
        return True
    from shapely.geometry import Point as SPoint
    for n in new_nodes:
        half, _ = _clearance_of(n)
        if n.head == "via":
            at = _node_xy(n, "at")
            if at is None:
                continue
            g = SPoint(at)
        else:
            s, e = _node_xy(n, "start"), _node_xy(n, "end")
            m = _node_xy(n, "mid")
            pts = [s] + ([m] if m else []) + [e] if s and e else []
            if len(pts) < 2:
                continue
            g = LineString(pts)
        try:
            if g.distance(edge) - half < min_mm:
                return False
        except Exception:
            return False
    return True


def signal_blockers_for_net(
    model: BoardModel,
    net: str,
    boxes: Sequence[Tuple[float, float, float, float]],
    protected_nets: Collection[str],
    extra_power: Collection[str] = (),
    max_victims: int = 2,
) -> Tuple[str, ...]:
    """Routed signal nets with copper inside ``boxes`` (sever candidates).

    Ground, effective-power (keyword or user-marked), protected, and
    the failed net itself are never victims; only real nets present in
    ``model.nets`` qualify. Smallest copper footprint first so the
    blast radius stays minimal.
    """
    boxes = tuple(boxes)
    if not boxes or int(max_victims) <= 0:
        return ()
    forbidden = set(protected_nets or ())

    def _ok(name: Optional[str]) -> bool:
        return (bool(name) and name != net and name in model.nets
                and name not in forbidden
                and not is_ground_net(name)
                and not is_power_net_effective(name, extra_power))

    def _hit(ax: float, ay: float, bx: float, by: float) -> bool:
        return any(_seg_hits_box(ax, ay, bx, by, b) for b in boxes)

    hits: Dict[str, int] = {}
    for t in model.tracks:
        if _ok(t.net_name) and _hit(t.start.x_mm, t.start.y_mm,
                                   t.end.x_mm, t.end.y_mm):
            hits[t.net_name] = hits.get(t.net_name, 0) + 1
    for a in model.arcs:
        if not _ok(a.net_name):
            continue
        pts = [(a.start.x_mm, a.start.y_mm), (a.mid.x_mm, a.mid.y_mm),
               (a.end.x_mm, a.end.y_mm)]
        if any(_hit(px, py, qx, qy)
               for (px, py), (qx, qy) in zip(pts, pts[1:])):
            hits[a.net_name] = hits.get(a.net_name, 0) + 1
    for v in model.vias:
        if not _ok(v.net_name):
            continue
        x, y = v.position.x_mm, v.position.y_mm
        if any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes):
            hits[v.net_name] = hits.get(v.net_name, 0) + 1
    ranked = sorted(hits, key=lambda n: (hits[n], n))
    return tuple(ranked[:int(max_victims)])


def retry_failed_with_shove(
    model: BoardModel,
    tree: SExpr,
    failed_nets: Sequence[str],
    protected_nets: Collection[str],
    params,
    radius_mm: float = 4.0,
    clearance_mm: Optional[float] = None,
    edge_mm: float = DEFAULT_EDGE_CLEARANCE_MM,
    progress_cb=None,
    should_cancel=None,
) -> Tuple[BoardModel, SExpr, Dict[str, ShoveReport]]:
    """Shove retry for GND-walled nets; strict-improvement-or-rollback.

    For each net (sequential, evolving board): clear inside-box GND
    from scratch copies, route the net alone, restore cleared GND that
    still clears the new copper. The net is adopted only when it routes
    AND new copper keeps edge clearance AND GND pad-components do not
    increase; when restore alone leaves GND split, one gap flight
    bridges the residual cuts before giving up (same machinery as
    signal Tier-2, arbitrated by the same fracture gate). Otherwise the
    net is fully rolled back (eviction was on copies, so rollback is
    free) and the board is untouched. Never touches signal/power
    copper. Cooperative cancel is honored between nets (and inside each
    solo route and gap flight via ``should_cancel``).
    """
    from dataclasses import replace as _replace_params

    if clearance_mm is None:
        clearance_mm = float(getattr(params, "clearance_mm", 0.2))
    # Shove opens local room: a net that still needs a huge search
    # after eviction is not GND-walled, so fail fast instead of
    # re-burning a full net budget per failed net. A 0 cap means
    # uncapped upstream; cap that case too.
    cap = int(getattr(params, "max_expansions_per_net", 0) or 0)
    params = _replace_params(params,
                             max_expansions_per_net=min(cap, 500_000)
                             if cap > 0 else 500_000)
    base_gnd = gnd_pad_components(model)
    reports: Dict[str, ShoveReport] = {}
    routed_here: Set[str] = set()

    for net in failed_nets:
        if should_cancel is not None and should_cancel():
            from .router import RoutingCancelled
            raise RoutingCancelled(f"cancelled before shove for {net}")
        rep = ShoveReport(net=net)
        boxes = boxes_for_net(model, net, radius_mm)
        if not boxes:
            rep.reason = "no pins"
            reports[net] = rep
            continue
        ev = evict_gnd_in_boxes(model, tree, boxes)
        rep.evicted = ev.evicted_count
        if ev.evicted_count == 0:
            rep.reason = "no GND in shove boxes"
            reports[net] = rep
            continue
        protected = set(protected_nets) | set(routed_here)
        result, _, _ = route_nets(
            model=ev.model, net_names=[net], protected_nets=protected,
            params=params, original_pcb_tree=ev.tree,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
        )
        if result.nets_routed != 1 or result.pcb_tree is None:
            rep.reason = (result.failed_reasons or {}).get(net, "still failing")
            reports[net] = rep
            continue
        new_nodes = new_copper_nodes(ev.tree, result.pcb_tree)
        if not edge_clearance_ok(new_nodes, model.edge_cuts, edge_mm):
            rep.reason = "edge-clearance rollback"
            reports[net] = rep
            continue
        post_model = board_model(result.pcb_tree)
        model2, tree2, restored, kept = _restore_cleared(
            post_model, result.pcb_tree, ev, new_nodes, clearance_mm)
        rep.restored = restored
        if gnd_pad_components(model2) > base_gnd:
            ok, model2, tree2, why = _reconnect_gnd_gap(
                model2, tree2, ev, boxes, kept, params, clearance_mm,
                edge_mm, base_gnd, progress_cb, should_cancel)
            if not ok:
                rep.reason = why
                reports[net] = rep
                continue
            rep.gap_routed = True
        model, tree = model2, tree2
        base_gnd = gnd_pad_components(model)
        routed_here.add(net)
        rep.routed = True
        reports[net] = rep
    return model, tree, reports


def retry_failed_with_signal_shove(
    model: BoardModel,
    tree: SExpr,
    failed_nets: Sequence[str],
    protected_nets: Collection[str],
    params,
    radius_mm: float = 4.0,
    clearance_mm: Optional[float] = None,
    edge_mm: float = DEFAULT_EDGE_CLEARANCE_MM,
    max_victims: int = 2,
    extra_power: Collection[str] = (),
    progress_cb=None,
    should_cancel=None,
) -> Tuple[BoardModel, SExpr, Dict[str, ShoveReport]]:
    """Signal shove: sever plus restore, then gap reconnect (Tier-2).

    For each net (sequential, evolving board): sever routed
    non-protected signal blockers inside per-pin boxes from scratch
    copies (same split ledger as GND shove, plus the box GND), route
    the net alone, restore cleared copper that still clears the new
    copper. The net is adopted when it routes AND new copper keeps edge
    clearance AND either every *victim* piece restores bit-identical
    (Tier-1; GND leftovers stay under the pad-components fracture
    gate, same tolerance as GND shove) or every unrestored victim
    reconnects across its gaps (Tier-2: cut points + pads as
    terminals, kept stubs as walkable seed, single-island coverage
    required) AND GND pad-components do not increase; otherwise the
    net is fully rolled back (eviction was on copies, so rollback is
    free). Ground, effective-power, protected, and already-fixed nets
    are never victims. Cooperative cancel is honored between nets (and
    inside each solo route and gap flight via ``should_cancel``).
    """
    from dataclasses import replace as _replace_params

    if clearance_mm is None:
        clearance_mm = float(getattr(params, "clearance_mm", 0.2))
    # Same fail-fast solo cap as GND shove: a net that still needs a
    # huge search after eviction is not box-walled.
    cap = int(getattr(params, "max_expansions_per_net", 0) or 0)
    params = _replace_params(params,
                             max_expansions_per_net=min(cap, 500_000)
                             if cap > 0 else 500_000)
    base_gnd = gnd_pad_components(model)
    reports: Dict[str, ShoveReport] = {}
    routed_here: Set[str] = set()

    for net in failed_nets:
        if should_cancel is not None and should_cancel():
            from .router import RoutingCancelled
            raise RoutingCancelled(f"cancelled before signal shove for {net}")
        rep = ShoveReport(net=net)
        boxes = boxes_for_net(model, net, radius_mm)
        if not boxes:
            rep.reason = "no pins"
            reports[net] = rep
            continue
        victims = signal_blockers_for_net(
            model, net, boxes, set(protected_nets) | set(routed_here),
            extra_power, max_victims)
        if not victims:
            rep.reason = "no signal blockers in shove boxes"
            reports[net] = rep
            continue
        rep.victims = tuple(victims)
        ev = evict_gnd_in_boxes(model, tree, boxes, victims=victims)
        rep.evicted = ev.evicted_count
        if ev.evicted_count == 0:
            rep.reason = "no blockers in shove boxes"
            reports[net] = rep
            continue
        protected = set(protected_nets) | set(routed_here)
        result, _, _ = route_nets(
            model=ev.model, net_names=[net], protected_nets=protected,
            params=params, original_pcb_tree=ev.tree,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
        )
        if result.nets_routed != 1 or result.pcb_tree is None:
            rep.reason = (result.failed_reasons or {}).get(net, "still failing")
            reports[net] = rep
            continue
        new_nodes = new_copper_nodes(ev.tree, result.pcb_tree)
        if not edge_clearance_ok(new_nodes, model.edge_cuts, edge_mm):
            rep.reason = "edge-clearance rollback"
            reports[net] = rep
            continue
        post_model = board_model(result.pcb_tree)
        model2, tree2, restored, _kept = _restore_cleared(
            post_model, result.pcb_tree, ev, new_nodes, clearance_mm)
        rep.restored = restored
        if not victim_pieces_intact(ev, new_nodes, victims, clearance_mm):
            ok, model2, tree2, why = _reconnect_victim_gaps(
                model2, tree2, ev, boxes, victims, params, clearance_mm,
                edge_mm, extra_power, progress_cb, should_cancel)
            if not ok:
                rep.reason = why
                reports[net] = rep
                continue
            rep.gap_routed = True
        if gnd_pad_components(model2) > base_gnd:
            rep.reason = "GND fracture rollback"
            reports[net] = rep
            continue
        model, tree = model2, tree2
        base_gnd = gnd_pad_components(model)
        routed_here.add(net)
        rep.routed = True
        reports[net] = rep
    return model, tree, reports
