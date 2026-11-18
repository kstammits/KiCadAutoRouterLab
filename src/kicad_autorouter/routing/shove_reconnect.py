from __future__ import annotations
"""Shove reconnect: gap repair + retry (split from shove.py)."""
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
from .shove_evict import Eviction, ShoveReport, _Middle, _clearance_of, _new_copper_clearance, _node_net, _node_xy, _restore_cleared, _seg_hits_box, _seg_node, _split_pieces, boxes_for_net, evict_gnd_in_boxes, gnd_pad_components, new_copper_nodes, pin_sites, victim_pieces_intact
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
