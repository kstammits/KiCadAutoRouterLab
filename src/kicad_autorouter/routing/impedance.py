"""Inlet-anchored DC-resistance screening for power and ground nets.

Report-only analysis (no routing behavior change): build a small
resistive graph from the routed board's discrete copper (pads, tracks,
vias) plus pour-island topology from :mod:`pour.labeled_copper`, then
run Dijkstra from a user-picked inlet pad to every pad on the evaluated
nets.

Resistance model (screening estimates, documented here and in the
report payload — not a field solver):

- Track: ``R = rho_cu * L / (w * t_cu)`` with ``rho_cu = 1.68e-8 Ω·m``
  and ``t_cu = 35 µm`` (1 oz outer). ``R_sq ≈ 0.48 mΩ/□``.
- Via / plated barrel: hollow-cylinder barrel
  ``R = rho * h / (pi * (r_o² - r_i²))`` with board thickness
  ``h = 1.6 mm`` and plating ``25 µm``. A 0.4 mm drill works out to
  ≈0.9 mΩ; same formula covers THT pad barrels by drill size.
- Pour tap: a pad/track/via cell sitting on island copper joins that
  island supernode through a small fixed tap (``POUR_TAP_MOHM``). Island
  interiors are treated as equipotential — thermal spokes are modeled
  as solid (as in ``pour.py``), which mildly *underestimates* R; the
  report notes this. Narrow-neck underestimation is bounded by the
  via-count proxy below rather than hidden.
- Via-count proxy (the chokepoint signal): every inter-layer hop
  increments ``vias_used``; a connected pad whose only inlet path uses
  a single via/hop is flagged ``spof`` even when its resistance passes.

Redundancy: edge-disjoint second path (primary-path edges removed,
Dijkstra re-run), capped at 2. ``disjoint: 0`` disconnected, ``1``
single path (SPOF), ``2`` well-connected.

Region-aware: pad nodes are keyed by footprint UUID (duplicate refs
such as mounting-hole arrays stay distinct), and every board region is
measured from an inlet in its own region — copper must never join
across panel regions (same rule as the stitcher), so one global inlet
cannot cover siblings. Regions without an inlet report ``no-inlet``.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional, Sequence, Set, Tuple

from ..board_model import BoardModel, copper_tracks, is_ground_net
from .grid import RoutingGrid, board_to_grid, create_grid_from_model
from .pour import labeled_copper

RHO_CU_OHM_M = 1.68e-8
T_CU_1OZ_M = 35e-6
R_SQ_MOHM = RHO_CU_OHM_M / T_CU_1OZ_M * 1e3  # ≈ 0.48 mΩ/□
BOARD_THICKNESS_MM = 1.6
VIA_PLATING_UM = 25.0
POUR_TAP_MOHM = 0.2
# Mated-pair contact resistance: a header mate is pin bulk (brass, a few
# mΩ) *plus* a spring-contact interface, typically spec'd at 10–20 mΩ max
# for 2.54 mm headers — an order of magnitude above a plated via barrel
# (~0.9 mΩ of continuous copper). Conservative screening default;
# override per bridge where the connector datasheet says otherwise.
BRIDGE_DEFAULT_MOHM = 20.0
SNAP_MM = 0.1

DEFAULT_GND_MAX_MOHM = 50.0  # screening floor, not a spec: must clear a
# known-good single header hop (~22 mΩ) with margin, while still catching
# real breaks (severed pours read in the hundreds of mΩ and up)
DEFAULT_POWER_MAX_MOHM = 250.0  # rails at tens of mA: hundreds of mΩ are fine


def track_resistance_mohm(length_mm: float, width_mm: float,
                           thickness_um: float = 35.0) -> float:
    """DC resistance of a rectangular track in mΩ."""
    if length_mm <= 0 or width_mm <= 0 or thickness_um <= 0:
        return 0.0
    t_m = thickness_um * 1e-6
    return RHO_CU_OHM_M * (length_mm * 1e-3) / ((width_mm * 1e-3) * t_m) * 1e3


def via_resistance_mohm(drill_mm: float, board_mm: float = BOARD_THICKNESS_MM,
                        plating_um: float = VIA_PLATING_UM) -> float:
    """DC resistance of a plated barrel in mΩ (hollow cylinder)."""
    if drill_mm <= 0 or board_mm <= 0 or plating_um <= 0:
        return 0.0
    r_o = drill_mm / 2.0
    wall = plating_um / 1e3  # mm
    r_i = max(r_o - wall, r_o * 0.1)
    area_mm2 = math.pi * (r_o * r_o - r_i * r_i)
    if area_mm2 <= 0:
        return 0.0
    return RHO_CU_OHM_M * (board_mm * 1e-3) / (area_mm2 * 1e-6) * 1e3


@dataclass(frozen=True)
class InletSpec:
    """User-picked inlet anchor: footprint ref + pad number."""

    ref: str
    pad: str


@dataclass(frozen=True)
class BridgeSpec:
    """Mated inter-panel pair: two pads shorted by assembly (pin headers,
    board-to-board connectors). ``r_mohm`` is the contact resistance
    (default ``BRIDGE_DEFAULT_MOHM`` — well above a via, see note there)."""

    a_ref: str
    a_pad: str
    b_ref: str
    b_pad: str
    r_mohm: float = BRIDGE_DEFAULT_MOHM


@dataclass
class PadImpedance:
    """Per-pad result (JSON-serializable via ``to_dict``).

    ``uuid`` keys the node (duplicate refs stay distinct); ``x_mm`` /
    ``y_mm`` / ``region`` / ``inlet`` drive the pad overlay.
    """

    ref: str
    pad: str
    net: str
    connected: bool
    r_mohm: Optional[float]
    uuid: str = ""
    x_mm: float = 0.0
    y_mm: float = 0.0
    region: int = -1
    inlet: str = ""
    vias_used: int = 0
    disjoint_paths: int = 0
    bridges_used: int = 0
    verdict: str = "fail"  # pass | attention | fail
    reason: str = ""
    path: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "pad": self.pad,
            "net": self.net,
            "connected": self.connected,
            "r_mohm": None if self.r_mohm is None else round(self.r_mohm, 3),
            "x_mm": round(self.x_mm, 3),
            "y_mm": round(self.y_mm, 3),
            "region": self.region,
            "inlet": self.inlet,
            "vias_used": self.vias_used,
            "disjoint_paths": self.disjoint_paths,
            "bridges_used": self.bridges_used,
            "spof": self.connected and self.disjoint_paths < 2,
            "verdict": self.verdict,
            "reason": self.reason,
            "path": list(self.path),
        }


@dataclass
class NetImpedance:
    net: str
    inlets: List[InletSpec] = field(default_factory=list)
    pads: List[PadImpedance] = field(default_factory=list)
    worst_mohm: Optional[float] = None
    failures: int = 0
    attention: int = 0

    def to_dict(self) -> dict:
        inlets = [{"ref": i.ref, "pad": i.pad} for i in self.inlets]
        return {
            "net": self.net,
            "inlets": inlets,
            "inlet": inlets[0] if inlets else None,
            "pads": [p.to_dict() for p in self.pads],
            "worst_mohm": self.worst_mohm,
            "failures": self.failures,
            "attention": self.attention,
        }


@dataclass
class ImpedanceReport:
    """Board-level screening result."""

    nets: List[NetImpedance] = field(default_factory=list)
    gnd_max_mohm: float = DEFAULT_GND_MAX_MOHM
    power_max_mohm: float = DEFAULT_POWER_MAX_MOHM
    assumptions: Dict[str, object] = field(default_factory=dict)
    bridges_explicit: List[dict] = field(default_factory=list)
    bridges_auto: int = 0
    bridges_cross_net: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "gnd_max_mohm": self.gnd_max_mohm,
            "power_max_mohm": self.power_max_mohm,
            "assumptions": self.assumptions,
            "bridges": {"explicit": self.bridges_explicit,
                        "auto": self.bridges_auto,
                        "cross_net": self.bridges_cross_net},
            "nets": [n.to_dict() for n in self.nets],
            "summary": {
                "nets_evaluated": len(self.nets),
                "pads_evaluated": sum(len(n.pads) for n in self.nets),
                "failures": sum(n.failures for n in self.nets),
                "attention": sum(n.attention for n in self.nets),
            },
        }


def _assumptions() -> Dict[str, object]:
    return {
        "rho_cu_ohm_m": RHO_CU_OHM_M,
        "cu_thickness_um": 35.0,
        "sheet_mohm_per_sq": round(R_SQ_MOHM, 3),
        "board_mm": BOARD_THICKNESS_MM,
        "via_plating_um": VIA_PLATING_UM,
        "pour_model": "island interiors equipotential; thermal spokes solid (mild underestimate)",
        "note": "screening estimate, not a field solver",
    }


def _pad_key(uuid: str, number: str) -> str:
    """Graph node for a pad — UUID-keyed so duplicate refs stay distinct."""
    return f"pad:{uuid}:{number}"


def _snap_key(x_mm: float, y_mm: float, layer: str) -> str:
    return f"pt:{layer}:{round(x_mm / SNAP_MM):d}:{round(y_mm / SNAP_MM):d}"


def _dijkstra(adj: Dict[str, List[Tuple[str, float, int, str, int]]],
              source: str,
              banned: frozenset = frozenset()) -> Tuple[Dict[str, float], Dict[str, Tuple[str, int, str, int]]]:
    dist: Dict[str, float] = {source: 0.0}
    prev: Dict[str, Tuple[str, int, str, int]] = {}
    heap: List[Tuple[float, str]] = [(0.0, source)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        for v, w, via_inc, kind, eid in adj.get(u, ()):
            if eid in banned:
                continue
            nd = d + w
            if nd < dist.get(v, math.inf) - 1e-12:
                dist[v] = nd
                prev[v] = (u, via_inc, kind, eid)
                heapq.heappush(heap, (nd, v))
    return dist, prev


def _reconstruct(prev: Dict[str, Tuple[str, int, str, int]], source: str,
                 target: str) -> Tuple[List[str], int, List[str], frozenset, frozenset]:
    """Rebuild the path; return (nodes, via_hops, kinds, ban_set, spare).

    ``ban_set`` holds the interior link edges for the disjoint-path probe;
    endpoint attachments (first/last hop, shared by construction) are
    returned as ``spare`` and must NOT be banned, or no second path could
    ever leave its endpoints.
    """
    nodes = [target]
    vias = 0
    eids: List[int] = []
    kinds: List[str] = []
    cur = target
    seen = {target}
    while cur != source:
        step = prev.get(cur)
        if step is None:
            return [], 0, [], frozenset(), frozenset()
        p, via_inc, kind, eid = step
        if p in seen:
            return [], 0, [], frozenset(), frozenset()
        seen.add(p)
        vias += via_inc
        kinds.append(kind)
        eids.append(eid)
        nodes.append(p)
        cur = p
    nodes.reverse()
    eids.reverse()
    kinds.reverse()
    used = {eid for eid, kind in zip(eids, kinds) if kind not in ("tap",)}
    spare = set()
    if eids:
        spare.add(eids[0])
        spare.add(eids[-1])
    return nodes, vias, kinds, frozenset(used - spare), frozenset(spare)


def _find_pad(model: BoardModel, ref: str, number: str):
    fp = model.by_ref.get(ref)
    if fp is None:
        return None, None
    for pad in fp.pads:
        if pad.number == number:
            return fp, pad
    return fp, None


def _island_at(labels, copper, grid: RoutingGrid, x_mm: float, y_mm: float) -> int:
    c, r = board_to_grid(grid, x_mm, y_mm)
    H, W = labels.shape
    if 0 <= r < H and 0 <= c < W and copper[r, c]:
        return int(labels[r, c])
    # Snap-window probe: pads/vias near (not exactly on) island copper
    # after sliver removal still tap the pour.
    rad = max(1, int(math.ceil(SNAP_MM / grid.resolution_mm)))
    r0, r1 = max(0, r - rad), min(H, r + rad + 1)
    c0, c1 = max(0, c - rad), min(W, c + rad + 1)
    win_lab = labels[r0:r1, c0:c1]
    win_cu = copper[r0:r1, c0:c1]
    if win_cu.any():
        vals = win_lab[win_cu]
        if len(vals):
            return int(vals[0])
    return 0


def _normalize_inlets(specs: Optional[Sequence[object]]) -> List[InletSpec]:
    out: List[InletSpec] = []
    for spec in specs or ():
        if isinstance(spec, InletSpec):
            out.append(spec)
        # Pad numbers may be "" (mounting holes): test presence, not truth.
        elif isinstance(spec, dict) and spec.get("ref") and isinstance(spec.get("pad"), str):
            out.append(InletSpec(ref=spec["ref"], pad=spec["pad"]))
    return out


def _normalize_bridges(specs: Optional[Sequence[object]]) -> List[BridgeSpec]:
    out: List[BridgeSpec] = []
    for spec in specs or ():
        if isinstance(spec, BridgeSpec):
            out.append(spec)
        elif isinstance(spec, dict):
            a, b = spec.get("a"), spec.get("b")
            if (isinstance(a, dict) and isinstance(b, dict) and a.get("ref") and b.get("ref")
                    and isinstance(a.get("pad"), str) and isinstance(b.get("pad"), str)):
                try:
                    r = float(spec.get("r_mohm", BRIDGE_DEFAULT_MOHM))
                except (TypeError, ValueError):
                    r = BRIDGE_DEFAULT_MOHM
                out.append(BridgeSpec(a_ref=a["ref"], a_pad=a.get("pad", ""),
                                      b_ref=b["ref"], b_pad=b.get("pad", ""),
                                      r_mohm=max(r, 0.0)))
    return out


def _region_of(model: BoardModel, uuid: str) -> int:
    if not model.board_regions:
        return 0
    return model.footprint_region.get(uuid, -1)


def suggest_header_mates(model: BoardModel,
                         tol: float = 0.15) -> List[dict]:
    """Candidate inter-panel header mates (board contacts).

    Pin headers (``J*`` refs with "header" in the footprint id) sitting at
    the same fractional position in different panel regions with equal pad
    counts mate pin-to-pin (matched by pad number). Same-net pairs are
    ready-made :class:`BridgeSpec` inputs; different-net pairs are equally
    applicable — net names often differ deliberately to fool KiCad's DRC
    while the pins are intended to link, so the engine honors declared
    mates across net names. The ``match`` flag just records whether the
    file already agrees.
    """
    if len(model.board_regions) < 2:
        return []
    bbox = {r.uuid: r.bbox for r in model.board_regions}
    headers = [fp for fp in model.footprints
               if (fp.ref or "").startswith("J")
               and "header" in (fp.footprint_id or "").lower()
               and len(fp.pads) >= 2]

    def _frac(fp) -> Optional[Tuple[float, float]]:
        idx = model.footprint_region.get(fp.uuid, -1)
        regs = list(model.board_regions)
        if idx is None or not (0 <= idx < len(regs)):
            return None
        minx, maxx, miny, maxy = regs[idx].bbox
        if maxx <= minx or maxy <= miny:
            return None
        return ((fp.x_mm - minx) / (maxx - minx),
                (fp.y_mm - miny) / (maxy - miny))

    out: List[dict] = []
    for i in range(len(headers)):
        for j in range(i + 1, len(headers)):
            fa, fb = headers[i], headers[j]
            if _region_of(model, fa.uuid) == _region_of(model, fb.uuid):
                continue
            if len(fa.pads) != len(fb.pads):
                continue
            pa, pb = _frac(fa), _frac(fb)
            if pa is None or pb is None:
                continue
            if abs(pa[0] - pb[0]) > tol or abs(pa[1] - pb[1]) > tol:
                continue
            nb = {p.number: p for p in fb.pads}
            for p in fa.pads:
                q = nb.get(p.number)
                if q is None:
                    continue
                out.append({
                    "a": {"ref": fa.ref, "pad": p.number},
                    "b": {"ref": fb.ref, "pad": q.number},
                    "net_a": p.net_name,
                    "net_b": q.net_name,
                    "match": (p.net_name is not None
                              and p.net_name == q.net_name),
                })
    return out


def _net_pads(model: BoardModel, net_name: str):
    """``(footprint, pad)`` pairs carrying ``net_name`` (UUID-distinct)."""
    out = []
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name == net_name:
                out.append((fp, pad))
    return out


def _multi_dijkstra(adj, sources: Sequence[str], banned=frozenset()):
    """Dijkstra from several inlet sources at once; tracks the feeding source."""
    dist: Dict[str, float] = {}
    prev: Dict[str, Tuple[str, int, str, int]] = {}
    origin: Dict[str, str] = {}
    heap: List[Tuple[float, str]] = []
    for source in sources:
        if source not in adj or source in dist:
            continue
        dist[source] = 0.0
        origin[source] = source
        heap.append((0.0, source))
    heapq.heapify(heap)
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        for v, w, via_inc, kind, eid in adj.get(u, ()):
            if eid in banned:
                continue
            nd = d + w
            if nd < dist.get(v, math.inf) - 1e-12:
                dist[v] = nd
                prev[v] = (u, via_inc, kind, eid)
                origin[v] = origin[u]
                heapq.heappush(heap, (nd, v))
    return dist, prev, origin


def analyze_power_gnd(
    model: BoardModel,
    grid: Optional[RoutingGrid] = None,
    gnd_inlets: Optional[Sequence[object]] = None,
    power_inlets: Optional[Dict[str, Sequence[object]]] = None,
    gnd_nets: Optional[List[str]] = None,
    power_nets: Optional[List[str]] = None,
    gnd_max_mohm: float = DEFAULT_GND_MAX_MOHM,
    power_max_mohm: float = DEFAULT_POWER_MAX_MOHM,
    bridges: Optional[Sequence[object]] = None,
    auto_bridges: bool = True,
    # Backward-compat single-inlet spell (wrapped into a one-item list).
    gnd_inlet: Optional[object] = None,
) -> ImpedanceReport:
    """Screening analysis: inlet-anchored resistance to every evaluated pad.

    ``gnd_nets`` defaults to all ground nets present on the board;
    ``power_nets`` defaults to ``power_inlets`` keys. All inlets feed one
    multi-source search per net, so each pad is measured from its lowest-R
    feed (parallel feeds count). Inter-panel assemblies join through
    ``bridges`` (user-declared mated pairs — honored across net names,
    since mating is an assembly fact the file cannot see) plus
    ``auto_bridges`` (overlapping same-net THT holes across regions, e.g.
    stacked mounting holes); without a bridge, panels stay electrically
    isolated and far pads report ``disconnected``. Nets without any inlet
    report all pads ``fail``/``no-inlet``.
    """
    gnd_specs = _normalize_inlets(gnd_inlets)
    if gnd_inlet is not None:
        gnd_specs = _normalize_inlets([gnd_inlet]) + gnd_specs
    power_specs = {n: _normalize_inlets(s) for n, s in (power_inlets or {}).items()}
    bridge_specs = _normalize_bridges(bridges)
    if grid is None:
        grid = create_grid_from_model(model, resolution_mm=0.2)
    if gnd_nets is None:
        gnd_nets = sorted({n for n in model.nets if is_ground_net(n)})
    if power_nets is None:
        power_nets = sorted(power_specs)

    report = ImpedanceReport(gnd_max_mohm=gnd_max_mohm,
                             power_max_mohm=power_max_mohm,
                             assumptions=_assumptions())
    report.bridges_explicit = [b.__dict__ for b in bridge_specs]
    # Copper scope: evaluated nets plus nets touched by bridges, so
    # cross-net mates bring their copper into the shared graph.
    bridge_nets: Set[str] = set()
    for spec in bridge_specs:
        for ref, num in ((spec.a_ref, spec.a_pad), (spec.b_ref, spec.b_pad)):
            _fp, _pad = _find_pad(model, ref, num)
            if _pad is not None and _pad.net_name:
                bridge_nets.add(_pad.net_name)
    nets_needed = (set(gnd_nets) | {n for n in power_nets} | bridge_nets)
    adj = _build_graph(model, grid, nets_needed)
    _applied, n_cross = _add_explicit_bridges(adj, model, bridge_specs)
    report.bridges_cross_net = n_cross
    if auto_bridges:
        report.bridges_auto = _add_auto_bridges(adj, model)
    jobs: List[Tuple[str, List[InletSpec], float]] = [
        (n, gnd_specs, gnd_max_mohm) for n in gnd_nets
    ] + [(n, power_specs.get(n, []), power_max_mohm) for n in power_nets
          if n not in set(gnd_nets)]
    for net_name, inlets, threshold in jobs:
        report.nets.append(_analyze_net(model, adj, net_name, inlets, threshold))
    return report


def _analyze_net(model: BoardModel, adj: Dict[str, List[Tuple[str, float, int, str, int]]],
                 net_name: str, inlets: Sequence[InletSpec],
                 threshold_mohm: float) -> NetImpedance:
    """Measure every pad of one net from the lowest-R inlet feed, over the
    shared board-wide graph (cross-net bridge hops included)."""
    result = NetImpedance(net=net_name, inlets=list(inlets))
    pads = _net_pads(model, net_name)
    if not pads:
        return result, 0

    def _fail(fp, pad, reason: str) -> PadImpedance:
        return PadImpedance(ref=fp.ref, pad=pad.number, net=net_name,
                            connected=False, r_mohm=None, uuid=fp.uuid,
                            x_mm=pad.position.x_mm, y_mm=pad.position.y_mm,
                            region=_region_of(model, fp.uuid),
                            verdict="fail", reason=reason)

    if not inlets:
        for fp, pad in pads:
            result.pads.append(_fail(fp, pad, "no-inlet"))
        result.failures = len(result.pads)
        return result

    # Resolve inlet specs to graph sources (deduped, first wins).
    sources: List[str] = []
    labels: Dict[str, str] = {}
    seen_specs = set()
    any_resolved = False
    for spec in inlets:
        if (spec.ref, spec.pad) in seen_specs:
            continue
        seen_specs.add((spec.ref, spec.pad))
        fp, pad = _find_pad(model, spec.ref, spec.pad)
        if fp is None or pad is None:
            continue
        if pad.net_name != net_name:
            for f2, p2 in pads:
                result.pads.append(_fail(f2, p2, "inlet-wrong-net"))
            result.failures = len(result.pads)
            return result
        any_resolved = True
        node = _pad_key(fp.uuid, pad.number)
        if node not in labels:
            labels[node] = f"{spec.ref}.{spec.pad}"
            sources.append(node)
    if not any_resolved:
        for fp, pad in pads:
            result.pads.append(_fail(fp, pad, "inlet-not-found"))
        result.failures = len(result.pads)
        return result

    dist, prev, origin = _multi_dijkstra(adj, sources)
    worst: Optional[float] = None
    for fp, pad in sorted(pads, key=lambda t: (t[0].ref, t[1].number)):
        region = _region_of(model, fp.uuid)
        target = _pad_key(fp.uuid, pad.number)
        base = dict(ref=fp.ref, pad=pad.number, net=net_name, uuid=fp.uuid,
                    x_mm=pad.position.x_mm, y_mm=pad.position.y_mm, region=region)
        if target not in dist:
            result.pads.append(PadImpedance(connected=False, r_mohm=None,
                                            verdict="fail",
                                            reason="disconnected", **base))
            continue
        source = origin[target]
        label = labels[source]
        if target == source:
            result.pads.append(PadImpedance(
                connected=True, r_mohm=0.0, vias_used=0, disjoint_paths=2,
                verdict="pass", reason="inlet", inlet=label,
                path=(source,), **base))
            worst = 0.0 if worst is None else min(worst, 0.0)
            continue
        nodes, vias_used, kinds, used, _spare = _reconstruct(prev, source, target)
        disjoint = 1
        if nodes:
            _d2, _p2, _o2 = _multi_dijkstra(adj, sources, banned=used)
            if target in _d2:
                disjoint = 2
        r = dist[target]
        worst = r if worst is None else max(worst, r)
        n_bridge = sum(1 for k in kinds if k == "bridge")
        if r > threshold_mohm:
            verdict, reason = "fail", "over-threshold"
        elif disjoint < 2:
            verdict, reason = "attention", "single-path"
        else:
            verdict, reason = "pass", ""
        result.pads.append(PadImpedance(
            connected=True, r_mohm=r, vias_used=vias_used,
            disjoint_paths=disjoint, bridges_used=n_bridge,
            verdict=verdict, reason=reason,
            inlet=label, path=tuple(nodes), **base))
    result.worst_mohm = None if worst is None else round(worst, 3)
    result.failures = sum(1 for p in result.pads if p.verdict == "fail")
    result.attention = sum(1 for p in result.pads if p.verdict == "attention")
    result.pads.sort(key=lambda p: (-1 if p.r_mohm is None else p.r_mohm),
                     reverse=True)
    return result


def _add_edge(adj: Dict[str, List[Tuple[str, float, int, str, int]]],
              a: str, b: str, r_mohm: float, via_inc: int, kind: str) -> int:
    if a == b:
        return -1
    _add_edge.counter = getattr(_add_edge, "counter", 0) + 1
    eid = _add_edge.counter
    adj.setdefault(a, []).append((b, r_mohm, via_inc, kind, eid))
    adj.setdefault(b, []).append((a, r_mohm, via_inc, kind, eid))
    return eid


def _add_explicit_bridges(adj, model: BoardModel,
                            bridge_specs: Sequence[BridgeSpec]) -> Tuple[int, int]:
    """Short declared mated pairs with contact resistance.

    Pairs are honored across net names: mating is an assembly fact the
    file cannot see (deliberately different net names only fool KiCad's
    DRC), so a declared mate joins whatever copper it touches. Returns
    (applied, cross_net) counts.
    """
    applied = 0
    cross = 0
    for spec in bridge_specs:
        fa, pa = _find_pad(model, spec.a_ref, spec.a_pad)
        fb, pb = _find_pad(model, spec.b_ref, spec.b_pad)
        if fa is None or pa is None or fb is None or pb is None:
            continue
        ka, kb = _pad_key(fa.uuid, pa.number), _pad_key(fb.uuid, pb.number)
        if ka in adj and kb in adj:
            _add_edge(adj, ka, kb, spec.r_mohm, 0, "bridge")
            applied += 1
            if pa.net_name != pb.net_name:
                cross += 1
    return applied, cross


def _add_auto_bridges(adj, model: BoardModel) -> int:
    """Short overlapping same-net THT holes in *different* regions (stacked
    mounting holes clamping two panels into one hole) with the shared
    barrel's resistance. Same-net only: a cross-net overlap would be a
    short, correctly left unjoined. Returns the auto-bridge count.
    """
    n_auto = 0
    if not model.board_regions:
        return n_auto
    # Spatial hash of THT pads by snap cell.
    cells: Dict[Tuple[int, int], list] = {}
    for fp in model.footprints:
        region = _region_of(model, fp.uuid)
        for pad in fp.pads:
            if (not pad.net_name or not pad.is_through_hole
                    or pad.drill_mm <= 0 or pad.pad_type != "thru_hole"):
                continue
            key = (round(pad.position.x_mm / SNAP_MM), round(pad.position.y_mm / SNAP_MM))
            cells.setdefault(key, []).append((fp, pad, region))
    for members in cells.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                (fa, pa, ra), (fb, pb, rb) = members[i], members[j]
                if ra == rb or fa.uuid == fb.uuid:
                    continue
                if pa.net_name != pb.net_name:
                    continue
                ka, kb = _pad_key(fa.uuid, pa.number), _pad_key(fb.uuid, pb.number)
                if ka not in adj or kb not in adj:
                    continue
                r = via_resistance_mohm(min(pa.drill_mm, pb.drill_mm))
                _add_edge(adj, ka, kb, r, 0, "bridge")
                n_auto += 1
    return n_auto


def _build_graph(model: BoardModel, grid: RoutingGrid,
                 nets: Collection[str]) -> Dict[str, List[Tuple[str, float, int, str, int]]]:
    """Board-wide resistive adjacency (pads + tracks + vias + pour islands).

    ``nets`` scopes which nets contribute copper — evaluated nets plus
    nets touched by bridges. Island nodes are net-qualified
    (``pour:{net}:{layer}:{i}``); declared bridges join across them, so a
    mated pair links whatever net names it touches.
    """
    nets_set = set(nets)
    adj: Dict[str, List[Tuple[str, float, int, str, int]]] = {}
    labels = {n: labeled_copper(model, grid, n) for n in nets_set}
    layers = list(grid.layers)
    net_pad_list = [(fp, pad) for fp in model.footprints for pad in fp.pads
                    if pad.net_name in nets_set]

    # Pad nodes.
    for fp, pad in net_pad_list:
        adj.setdefault(_pad_key(fp.uuid, pad.number), [])

    # Track segments chained through same-net pads/vias touching their
    # interior (T-junctions): pad/via snap nodes split the segment so
    # mid-track taps read the true path instead of "disconnected".
    tap_points: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}
    for fp, pad in net_pad_list:
        pad_layers = [fp.layer] if not pad.is_through_hole else list(layers)
        for layer in pad_layers:
            tap_points.setdefault((layer, pad.net_name), []).append(
                (pad.position.x_mm, pad.position.y_mm))
    for via in model.vias:
        if via.net_name in nets_set:
            for layer in layers:
                tap_points.setdefault((layer, via.net_name), []).append(
                    (via.position.x_mm, via.position.y_mm))
    for track in copper_tracks(model):
        if track.net_name not in nets_set:
            continue
        x1, y1 = track.start.x_mm, track.start.y_mm
        x2, y2 = track.end.x_mm, track.end.y_mm
        dx, dy = x2 - x1, y2 - y1
        seg2 = dx * dx + dy * dy
        splits: Dict[str, float] = {}
        if seg2 > 0:
            for (px, py) in tap_points.get((track.layer, track.net_name), ()):
                t = ((px - x1) * dx + (py - y1) * dy) / seg2
                if 0.0 < t < 1.0 and abs((y2 - y1) * px - (x2 - x1) * py
                                          + x2 * y1 - y2 * x1) / math.sqrt(seg2) <= SNAP_MM * 1.5:
                    splits[_snap_key(px, py, track.layer)] = t
        chain = ([(0.0, _snap_key(x1, y1, track.layer))]
                 + [(t, k) for k, t in splits.items()]
                 + [(1.0, _snap_key(x2, y2, track.layer))])
        chain.sort(key=lambda item: item[0])
        total = math.sqrt(seg2)
        for (t0, ka), (t1, kb) in zip(chain, chain[1:]):
            adj.setdefault(ka, [])
            adj.setdefault(kb, [])
            _add_edge(adj, ka, kb,
                      track_resistance_mohm(max(t1 - t0, 0.0) * total,
                                            track.width_mm),
                      0, "track")

    # Via nodes: inter-layer hop with barrel resistance.
    for via in model.vias:
        if via.net_name not in nets_set:
            continue
        r = via_resistance_mohm(via.drill_mm)
        for layer in layers:
            adj.setdefault(_snap_key(via.position.x_mm, via.position.y_mm, layer), [])
        if len(layers) >= 2:
            _add_edge(adj,
                      _snap_key(via.position.x_mm, via.position.y_mm, layers[0]),
                      _snap_key(via.position.x_mm, via.position.y_mm, layers[1]),
                      r, 1, "via")

    # THT pad barrels: same-layer point already at the pad; add the
    # inter-layer hop so pours/vias can cross through the barrel.
    for fp, pad in net_pad_list:
        if not pad.is_through_hole or pad.drill_mm <= 0:
            continue
        if len(layers) >= 2 and pad.pad_type == "thru_hole":
            r = via_resistance_mohm(pad.drill_mm)
            _add_edge(adj,
                      _snap_key(pad.position.x_mm, pad.position.y_mm, layers[0]),
                      _snap_key(pad.position.x_mm, pad.position.y_mm, layers[1]),
                      r, 1, "barrel")

    # Discrete copper ↔ pad/tap joints (zero-R snap unions).
    def _union(point_key: str, pad_key: str) -> None:
        _add_edge(adj, point_key, pad_key, 0.0, 0, "tap")

    for fp, pad in net_pad_list:
        key = _pad_key(fp.uuid, pad.number)
        pad_layers = [fp.layer] if not pad.is_through_hole else list(layers)
        for layer in pad_layers:
            _union(_snap_key(pad.position.x_mm, pad.position.y_mm, layer), key)

    # Pour islands: supernodes (net-qualified); taps where discrete
    # copper sits on copper.
    island_node: Dict[Tuple[str, str, int], str] = {}
    for net in nets_set:
        copper_lab = labels[net]
        for layer in layers:
            copper, lab, count = copper_lab[layer]
            for i in range(1, count + 1):
                node = f"pour:{net}:{layer}:{i}"
                island_node[(net, layer, i)] = node
                adj.setdefault(node, [])

    def _tap_island(net: str, x_mm: float, y_mm: float, layer: str, other: str) -> None:
        copper, lab, _count = labels[net][layer]
        isl = _island_at(lab, copper, grid, x_mm, y_mm)
        if isl > 0:
            _add_edge(adj, other, island_node[(net, layer, isl)],
                      POUR_TAP_MOHM, 0, "pour")

    for fp, pad in net_pad_list:
        key = _pad_key(fp.uuid, pad.number)
        pad_layers = [fp.layer] if not pad.is_through_hole else list(layers)
        for layer in pad_layers:
            _tap_island(pad.net_name, pad.position.x_mm, pad.position.y_mm, layer, key)
    for track in copper_tracks(model):
        if track.net_name not in nets_set:
            continue
        for (x, y) in ((track.start.x_mm, track.start.y_mm),
                       (track.end.x_mm, track.end.y_mm)):
            _tap_island(track.net_name, x, y, track.layer,
                        _snap_key(x, y, track.layer))
    for via in model.vias:
        if via.net_name not in nets_set:
            continue
        for layer in layers:
            _tap_island(via.net_name, via.position.x_mm, via.position.y_mm, layer,
                        _snap_key(via.position.x_mm, via.position.y_mm, layer))
    return adj
