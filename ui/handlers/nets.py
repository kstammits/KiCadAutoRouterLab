"""Nets / inlets / bridge-suggestions HTTP endpoints.

Every status code + body is defined here.
"""

from __future__ import annotations

import json
import logging

from ui.server import (
    STATE,
    load_routing_params,
)

from kicad_autorouter.board_model import (
    is_ground_net,
    is_power_net_effective,
)
from kicad_autorouter.mass import courtyard_bbox_area_m2
from kicad_autorouter.placement.model import (
    collect_pad_nodes,
    find_decap_chip_links,
)
from kicad_autorouter.routing.impedance import (
    BRIDGE_DEFAULT_MOHM,
    suggest_header_mates,
)
from kicad_autorouter.routing.pipeline import resolve_track_width
from kicad_autorouter.routing.power import identify_power_nets
from kicad_autorouter.svg_render import decap_fp_uuids, net_colors

logger = logging.getLogger(__name__)


def handle_get(path, qs, handler):
    """GET dispatch for /api/nets/*.

    Returns ``(code, body: bytes, content_type)`` or ``None``.
    """
    if path == "/api/nets":
        return _handle_nets_get()

    if path == "/api/nets/terminals":
        return _handle_terminals(qs)

    if path == "/api/nets/inlets":
        return 200, json.dumps({"ok": True, **STATE.get_inlets()}).encode(), "application/json"

    if path == "/api/nets/bridge-suggestions":
        model, _, _, _, _, _ = STATE.snapshot
        if model is None:
            return 404, b"no board loaded", "text/plain"
        try:
            mates = suggest_header_mates(model)
        except Exception as exc:
            return 500, f"suggestion failed: {exc}".encode(), "text/plain"
        return 200, json.dumps({"ok": True, "mates": mates}).encode(), "application/json"

    if path == "/api/nets/protected":
        protected = STATE.get_protected_nets()
        return 200, json.dumps({"ok": True, "protected": sorted(protected)}).encode(), "application/json"

    if path == "/api/nets/power":
        extra = STATE.get_power_override_nets()
        return 200, json.dumps({"ok": True, "power_override": sorted(extra)}).encode(), "application/json"

    return None


def handle_post(path, qs, body, handler):
    """POST dispatch for /api/nets/*.

    Returns ``(code, body: bytes, content_type)`` or ``None``.
    """
    if path == "/api/nets/protected":
        return _handle_protected_post(body)

    if path == "/api/nets/power":
        return _handle_power_post(body)

    if path == "/api/nets/inlets":
        return _handle_inlets_post(body)

    return None


# --------------------------------------------------------------------------- #
# GET /api/nets
# --------------------------------------------------------------------------- #

def _mst_length_mm(points) -> float:
    """Euclidean MST total over ``[(x_mm, y_mm)]`` (Prim's, O(n^2)).

    Airwire difficulty proxy: 0 for fewer than 2 pads.
    """
    n = len(points)
    if n < 2:
        return 0.0
    import math
    in_tree = [False] * n
    in_tree[0] = True
    best = [math.hypot(points[i][0] - points[0][0],
                        points[i][1] - points[0][1]) for i in range(n)]
    total = 0.0
    for _ in range(n - 1):
        pick, pick_d = -1, math.inf
        for i in range(n):
            if not in_tree[i] and best[i] < pick_d:
                pick, pick_d = i, best[i]
        if pick < 0:
            break
        in_tree[pick] = True
        total += pick_d
        for i in range(n):
            if not in_tree[i]:
                d = math.hypot(points[i][0] - points[pick][0],
                               points[i][1] - points[pick][1])
                if d < best[i]:
                    best[i] = d
    return total


def _handle_nets_get():
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    protected = STATE.get_protected_nets()
    extra_power = STATE.get_power_override_nets()
    last_failed, last_failed_version = STATE.get_last_failed()
    rp = load_routing_params()
    try:
        via_cost_mm = float(getattr(rp, "via_cost_mm", 5.0) or 0.0)
    except (TypeError, ValueError):
        via_cost_mm = 5.0
    track_counts = {}
    arc_counts = {}
    via_counts = {}
    copper_len = {}
    import math
    for t in model.tracks:
        if t.net_name:
            track_counts[t.net_name] = track_counts.get(t.net_name, 0) + 1
            try:
                d = math.hypot(t.end.x_mm - t.start.x_mm,
                               t.end.y_mm - t.start.y_mm)
            except (AttributeError, TypeError):
                d = 0.0
            copper_len[t.net_name] = copper_len.get(t.net_name, 0.0) + d
    for a in model.arcs:
        if a.net_name:
            arc_counts[a.net_name] = arc_counts.get(a.net_name, 0) + 1
            try:
                d = (math.hypot(a.mid.x_mm - a.start.x_mm,
                                a.mid.y_mm - a.start.y_mm)
                     + math.hypot(a.end.x_mm - a.mid.x_mm,
                                  a.end.y_mm - a.mid.y_mm))
            except (AttributeError, TypeError):
                d = 0.0
            copper_len[a.net_name] = copper_len.get(a.net_name, 0.0) + d
    for v in model.vias:
        if v.net_name:
            via_counts[v.net_name] = via_counts.get(v.net_name, 0) + 1
    # Pad positions + board regions per net (airwire MST + bridge hint).
    multi = len(getattr(model, "board_regions", ()) or ()) > 1
    pad_points: dict = {}
    pad_regions: dict = {}
    region_of = getattr(model, "footprint_region", {}) or {}
    for fp in model.footprints:
        region = region_of.get(fp.uuid, -1) if multi else 0
        for pad in fp.pads or ():
            if not pad.net_name:
                continue
            try:
                xy = (float(pad.position.x_mm), float(pad.position.y_mm))
            except (AttributeError, TypeError, ValueError):
                continue
            pad_points.setdefault(pad.net_name, []).append(xy)
            pad_regions.setdefault(pad.net_name, set()).add(region)
    # Nets touched by a user-declared mated-pair bridge (by_ref lookup).
    bridged_nets = set()
    try:
        for b in (STATE.get_inlets().get("bridges") or []):
            for side in (b.get("a"), b.get("b")):
                if not isinstance(side, dict):
                    continue
                fp = (model.by_ref or {}).get(side.get("ref"))
                pads = fp.pads if fp is not None else ()
                hit = next((p for p in pads
                            if p.number == side.get("pad")), None)
                if hit is not None and hit.net_name:
                    bridged_nets.add(hit.net_name)
    except (AttributeError, TypeError):
        pass

    nets_data = []
    colors = net_colors(model, extra_power)
    decap_by_net = {}
    for fp in model.footprints:
        if not fp.uuid or not fp.pads:
            continue
        if not (fp.ref or "").upper().startswith("C"):
            continue
        nets = [p.net_name for p in fp.pads if p.net_name]
        if len(nets) != 2:
            continue
        sides = sorted(
            "power" if is_power_net_effective(n, extra_power) else ("ground" if is_ground_net(n) else "signal")
            for n in nets
        )
        if sides == ["ground", "power"]:
            for n in nets:
                decap_by_net.setdefault(n, []).append(fp.ref)
    try:
        _pads, _pad_to_fp, _, _, _ = collect_pad_nodes(model)
        _ref_by: dict = {}
        for _i, _fi in enumerate(_pad_to_fp):
            if _fi >= 0 and _fi not in _ref_by:
                _ref_by[_fi] = model.footprints[_fi].ref
        _areas = {}
        for _fi, _fp in enumerate(model.footprints):
            try:
                _areas[_fi] = courtyard_bbox_area_m2(_fp)
            except (AttributeError, TypeError, ValueError):
                continue
        _, decap_links = find_decap_chip_links(
            _pads, _pad_to_fp, _ref_by,
            fp_area_m2_by_idx=_areas,
            extra_power=extra_power,
        )
    except (AttributeError, TypeError, ValueError):
        decap_links = {}
    for net_name, conns in model.nets.items():
        footprint_refs = set()
        for conn in conns:
            footprint_refs.add(conn.ref)
        copper = round(copper_len.get(net_name, 0.0), 2)
        airwire = round(_mst_length_mm(pad_points.get(net_name, [])), 2)
        nvias = via_counts.get(net_name, 0)
        cost = round(copper + nvias * via_cost_mm, 2)
        regions = sorted(pad_regions.get(net_name, set()))
        is_multi = multi and len(regions) > 1
        is_bridged = net_name in bridged_nets
        nets_data.append({
            "name": net_name,
            "pad_count": len(conns),
            "footprint_count": len(footprint_refs),
            "track_count": track_counts.get(net_name, 0),
            "arc_count": arc_counts.get(net_name, 0),
            "via_count": nvias,
            "protected": net_name in protected,
            "is_power": is_power_net_effective(net_name, extra_power),
            "is_ground": is_ground_net(net_name),
            "power_user": net_name in extra_power,
            "width_mm": round(resolve_track_width(model, rp, net_name, extra_power), 3),
            "color": colors.get(net_name, "#8a93a0"),
            "decap_refs": sorted(decap_by_net.get(net_name, [])),
            "decap_links": {
                r: decap_links.get(r)
                for r in sorted(decap_by_net.get(net_name, []))
                if r in decap_links
            },
            "airwire_len_mm": airwire,
            "copper_len_mm": copper,
            "copper_cost_mm": cost,
            "via_cost_mm": via_cost_mm,
            "regions": regions,
            "region_count": len(regions),
            "multi_region": is_multi,
            "bridged": is_bridged,
            "needs_bridge": bool(is_multi and not is_bridged),
            "failed_reason": last_failed.get(net_name),
            "failed_version": last_failed_version if net_name in last_failed else None,
        })
    nets_data.sort(key=lambda n: (not n["protected"], -n["pad_count"]))
    body = json.dumps(
        {"ok": True, "nets": nets_data, "decap_links": decap_links}
    ).encode()
    return 200, body, "application/json"


def _handle_terminals(qs):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    net_name = (qs.get("net", [""])[0] or "")
    if net_name not in model.nets:
        return 404, f"unknown net: {net_name}".encode(), "text/plain"
    terms = []
    multi = len(model.board_regions) > 1
    for fp in model.footprints:
        region = model.footprint_region.get(fp.uuid, -1) if multi else 0
        for pad in fp.pads:
            if pad.net_name == net_name:
                terms.append({"x_mm": pad.position.x_mm,
                              "y_mm": pad.position.y_mm,
                              "ref": fp.ref, "pad": pad.number,
                              "region": region})
    body = json.dumps({"ok": True, "net": net_name,
                       "count": len(terms),
                       "multi_region": multi,
                       "terminals": terms}).encode()
    return 200, body, "application/json"


# --------------------------------------------------------------------------- #
# POST /api/nets/protected
# --------------------------------------------------------------------------- #

def _handle_protected_post(body):
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, b"invalid JSON", "text/plain"
    net_name = req.get("net")
    protected_flag = req.get("protected")
    if not net_name:
        return 400, b"net name required", "text/plain"
    with STATE._lock:
        if protected_flag:
            STATE.protected_nets.add(net_name)
        else:
            STATE.protected_nets.discard(net_name)
        version = STATE.version + 1
        STATE.version = version
    protected = STATE.get_protected_nets()
    return 200, json.dumps({
        "ok": True, "protected": sorted(protected), "version": version,
    }).encode(), "application/json"


# --------------------------------------------------------------------------- #
# POST /api/nets/power
# --------------------------------------------------------------------------- #

def _handle_power_post(body):
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, b"invalid JSON", "text/plain"
    net_name = req.get("net")
    if not net_name or "power" not in req:
        return 400, b"net name and power flag required", "text/plain"
    version = STATE.set_power_override_net(net_name, bool(req.get("power")))
    extra = STATE.get_power_override_nets()
    return 200, json.dumps({
        "ok": True, "power_override": sorted(extra), "version": version,
    }).encode(), "application/json"


# --------------------------------------------------------------------------- #
# POST /api/nets/inlets
# --------------------------------------------------------------------------- #

def _handle_inlets_post(body):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, b"invalid JSON", "text/plain"

    def _check_anchor(anchor):
        if not (isinstance(anchor, dict) and anchor.get("ref")
                and isinstance(anchor.get("pad"), str)):
            return None
        fp = model.by_ref.get(anchor["ref"])
        pad = next((p for p in fp.pads if p.number == anchor["pad"]), None) if fp else None
        return {"ref": anchor["ref"], "pad": anchor["pad"]} if (fp and pad) else None

    def _check_list(value):
        items = value if isinstance(value, list) else [value]
        checked = []
        for anchor in items:
            ok = _check_anchor(anchor)
            if ok is None:
                return None
            if ok not in checked:
                checked.append(ok)
        return checked

    if req.get("gnd_clear"):
        STATE.set_gnd_inlets([])
    if "bridges_clear" in req and req.get("bridges_clear"):
        STATE.set_bridges([])
    if "gnd_list" in req:
        g = req.get("gnd_list")
        if g is None:
            STATE.set_gnd_inlets([])
        else:
            checked = _check_list(g)
            if checked is None:
                return 400, b"gnd_list must be {ref, pad}, a list, or null", "text/plain"
            STATE.set_gnd_inlets(checked)
    if "gnd_add" in req:
        ok = _check_anchor(req.get("gnd_add"))
        if ok is None:
            return 400, b"gnd_add must be {ref, pad}", "text/plain"
        STATE.add_gnd_inlet(ok["ref"], ok["pad"])
    if "power" in req:
        p = req.get("power")
        if not isinstance(p, dict):
            return 400, b"power must be an object", "text/plain"
        for net_name, anchor in p.items():
            if net_name not in model.nets:
                return 400, f"unknown net {net_name}".encode(), "text/plain"
            if anchor is None:
                STATE.set_power_inlets(net_name, [])
            else:
                checked = _check_list(anchor)
                if checked is None:
                    return 400, f"bad inlet anchor for {net_name}".encode(), "text/plain"
                STATE.set_power_inlets(net_name, checked)
    if "bridges" in req:
        b = req.get("bridges")
        if b is None:
            STATE.set_bridges([])
        elif not isinstance(b, list):
            return 400, b"bridges must be a list or null", "text/plain"
        else:
            checked_bridges = []
            for item in b:
                if not (isinstance(item, dict) and isinstance(item.get("a"), dict)
                        and isinstance(item.get("b"), dict)):
                    return 400, b"bridge must be {a: {ref,pad}, b: {ref,pad}}", "text/plain"
                a_ok = _check_anchor(item["a"])
                b_ok = _check_anchor(item["b"])
                if a_ok is None or b_ok is None:
                    return 400, b"unknown bridge ref/pad", "text/plain"
                try:
                    r = float(item.get("r_mohm", BRIDGE_DEFAULT_MOHM))
                except (TypeError, ValueError):
                    r = BRIDGE_DEFAULT_MOHM
                checked_bridges.append({"a": a_ok, "b": b_ok, "r_mohm": max(r, 0.0)})
            STATE.set_bridges(checked_bridges)
    inlets = STATE.get_inlets()
    return 200, json.dumps({"ok": True, **inlets, "version": STATE.version}).encode(), "application/json"
