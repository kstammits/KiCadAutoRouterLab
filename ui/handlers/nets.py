"""Nets / inlets / bridge-suggestions HTTP endpoints.

Extracted from ui/server.py (Step 3: split god modules). Behavior-neutral move
only; every status code + body is unchanged.
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

def _handle_nets_get():
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    protected = STATE.get_protected_nets()
    extra_power = STATE.get_power_override_nets()
    rp = load_routing_params()
    track_counts = {}
    arc_counts = {}
    via_counts = {}
    for t in model.tracks:
        if t.net_name:
            track_counts[t.net_name] = track_counts.get(t.net_name, 0) + 1
    for a in model.arcs:
        if a.net_name:
            arc_counts[a.net_name] = arc_counts.get(a.net_name, 0) + 1
    for v in model.vias:
        if v.net_name:
            via_counts[v.net_name] = via_counts.get(v.net_name, 0) + 1

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
        nets_data.append({
            "name": net_name,
            "pad_count": len(conns),
            "footprint_count": len(footprint_refs),
            "track_count": track_counts.get(net_name, 0),
            "arc_count": arc_counts.get(net_name, 0),
            "via_count": via_counts.get(net_name, 0),
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
    if "gnd" in req:
        g = req.get("gnd")
        if g is None:
            STATE.set_gnd_inlets([])
        else:
            checked = _check_list(g)
            if checked is None:
                return 400, b"gnd must be {ref, pad}, a list, or null", "text/plain"
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
