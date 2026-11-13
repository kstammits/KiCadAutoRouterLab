"""Routing HTTP endpoints.

Every status code + body is defined here.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid as uuidlib
from collections import Counter as _Counter
from urllib.parse import parse_qs

from ui.server import (
    STATE,
    load_routing_params,
    save_routing_params,
)

from ui.server_jobs import (
    _JOBS,
    _JOBS_LOCK,
    _ROUTING_JOB_LOCK,
    _drc_lock,
    _prune_jobs_locked,
    _run_drc_now,
    _run_polish_job,
    _run_routing_job,
)
from ui.debug_grids import _cost_grid_to_debug, _secondary_to_debug

from kicad_autorouter.board_model import board_model, is_ground_net
from kicad_autorouter.io import rip_up_nets
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    resolve_clearance,
    resolve_track_width,
    terminals_for_net,
)
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.obstacles import build_fanout_cost
from kicad_autorouter.routing.impedance import (
    BRIDGE_DEFAULT_MOHM,
    DEFAULT_GND_MAX_MOHM,
    DEFAULT_POWER_MAX_MOHM,
    InletSpec,
    analyze_power_gnd,
)
from kicad_autorouter.routing.pour import gnd_islands
from kicad_autorouter.routing.power import identify_power_nets
from kicad_autorouter.strategy import (
    MODES,
    MODE_PRESETS,
    apply_mode,
    mode_owned_keys,
)

logger = logging.getLogger(__name__)


def _require_json(body):
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _impedance_report(qs, model):
    def _qfloat(name, default):
        try:
            return float((qs.get(name) or [default])[0])
        except (TypeError, ValueError):
            return default

    gnd_max = _qfloat("gnd_max_mohm", DEFAULT_GND_MAX_MOHM)
    power_max = _qfloat("power_max_mohm", DEFAULT_POWER_MAX_MOHM)
    nets_filter = None
    if qs.get("nets"):
        nets_filter = [n for n in (qs.get("nets") or [""])[0].split(",") if n]
    inlets = STATE.get_inlets()
    gnd_inlets = [InletSpec(**a) for a in (inlets.get("gnd_list") or [])]
    power_inlets = {n: [InletSpec(**a) for a in (lst or [])]
                    for n, lst in (inlets.get("power") or {}).items()}
    extra_power = STATE.get_power_override_nets()
    _power, _ground, _signal = identify_power_nets(model, extra_power)
    gnd_nets = [n for n in model.nets if is_ground_net(n)]
    if nets_filter is not None:
        gnd_nets = [n for n in gnd_nets if n in nets_filter]
    if str((qs.get("include_all_power") or [""])[0]).lower() in ("1", "true", "yes"):
        for n in _power:
            power_inlets.setdefault(n, [])
    power_nets = [n for n, lst in power_inlets.items() if lst]
    if nets_filter is not None:
        power_nets = [n for n in power_nets if n in nets_filter]
    auto = str((qs.get("auto_bridges") or ["1"])[0]).lower() not in ("0", "false", "no")
    grid = create_grid_from_model(model, resolution_mm=0.2)
    report = analyze_power_gnd(
        model, grid,
        gnd_inlets=gnd_inlets,
        power_inlets={n: lst for n, lst in power_inlets.items() if lst},
        gnd_nets=gnd_nets,
        power_nets=power_nets,
        gnd_max_mohm=gnd_max,
        power_max_mohm=power_max,
        bridges=STATE.get_inlets().get("bridges") or [],
        auto_bridges=auto,
    )
    return report.to_dict()


def handle_get(path, qs, handler):
    """GET dispatch for /api/routing/* and /api/drc/status.

    Returns ``(code, body: bytes, content_type)`` or ``None``.
    """
    if path == "/api/routing/params":
        params = load_routing_params()
        return 200, json.dumps({"ok": True, "params": params.to_dict()}).encode(), "application/json"

    if path == "/api/routing/debug/cost-grid":
        model, _, _, _, _, _ = STATE.snapshot
        if model is None:
            return 404, b"no board loaded", "text/plain"
        if STATE.cost_grid is None or STATE.routing_grid is None:
            return 404, b"no cost grid available (run routing first)", "text/plain"
        payload = _cost_grid_to_debug(STATE.cost_grid, STATE.routing_grid)
        net_name = (qs.get("net", [""])[0] or "")
        if net_name:
            if net_name not in model.nets:
                return 404, f"unknown net: {net_name}".encode(), "text/plain"
            try:
                params = load_routing_params()
                width = resolve_track_width(model, params, net_name, ())
                clear = resolve_clearance(model, params, net_name)
                secondary = build_fanout_cost(
                    model, STATE.routing_grid, net_name,
                    clearance_mm=clear, track_half_mm=width / 2.0)
                terminals = terminals_for_net(model, STATE.routing_grid, net_name)
            except Exception as exc:
                return 500, f"secondary build failed: {exc}".encode(), "text/plain"
            payload["net"] = net_name
            payload["secondary"] = _secondary_to_debug(
                secondary, STATE.routing_grid, terminals)
        return 200, json.dumps(payload).encode(), "application/json"

    if path == "/api/routing/debug/gnd-islands":
        model, _, _, _, _, _ = STATE.snapshot
        if model is None:
            return 404, b"no board loaded", "text/plain"
        try:
            grid = create_grid_from_model(model, resolution_mm=0.2)
            islands = gnd_islands(model, grid)
        except Exception as exc:
            return 500, f"island analysis failed: {exc}".encode(), "text/plain"
        layers = {}
        for layer, items in islands.items():
            layers[layer] = [{
                "net": isl.net_name,
                "area_mm2": round(isl.area_mm2, 2),
                "cx_mm": round(isl.centroid_mm[0], 3),
                "cy_mm": round(isl.centroid_mm[1], 3),
                "cells": isl.cell_count,
                "connected": isl.has_connection,
                "bbox_mm": [round(v, 3) for v in isl.bbox_mm],
                "zones": list(isl.zone_uuids),
            } for isl in items]
        payload = {
            "ok": True,
            "resolution_mm": grid.resolution_mm,
            "origin_mm": list(grid.origin_mm),
            "layers": layers,
        }
        return 200, json.dumps(payload).encode(), "application/json"

    if path == "/api/routing/debug/impedance":
        model, _, _, _, _, _ = STATE.snapshot
        if model is None:
            return 404, b"no board loaded", "text/plain"
        try:
            report = _impedance_report(qs, model)
        except Exception as exc:
            return 500, f"impedance analysis failed: {exc}".encode(), "text/plain"
        return 200, json.dumps(report).encode(), "application/json"

    if path == "/api/drc/status":
        return 200, json.dumps(STATE.get_drc_status()).encode(), "application/json"

    if path.startswith("/api/routing/jobs/"):
        job_id = path[len("/api/routing/jobs/"):]
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            snapshot = dict(job) if job else None
            if snapshot is not None:
                snapshot["progress"] = dict(job.get("progress") or {})
                if job.get("result") is not None:
                    snapshot["result"] = job["result"]
        if snapshot is None:
            return 404, b"unknown job", "text/plain"
        return 200, json.dumps(snapshot).encode(), "application/json"

    return None


def handle_post(path, qs, body, handler):
    """POST dispatch for /api/routing/* and /api/drc.

    Returns ``(code, body: bytes, content_type)`` or ``None``.
    """
    if path == "/api/drc":
        with _drc_lock:
            tree, current_version = STATE.snapshot_pcb()
            if tree is None:
                return 404, b"no board loaded", "text/plain"
            status = STATE.get_drc_status()
            if (
                not status["running"]
                and status["for_version"] == current_version
                and status["error"] is None
            ):
                by_type = {}
                for v in STATE.get_drc_violations():
                    t = v.get("type", "unknown")
                    by_type[t] = by_type.get(t, 0) + 1
                payload = {
                    "ok": True, "cached": True,
                    "for_version": current_version,
                    "current_version": current_version,
                    "stale": False,
                    "violations": status["violations"],
                    "unconnected": status["unconnected"],
                    "by_type": by_type,
                    "error": None,
                }
            else:
                payload = _run_drc_now(tree, current_version)
        return 200, json.dumps(payload).encode(), "application/json"

    if path == "/api/routing/params":
        req = _require_json(body)
        if req is None or not isinstance(req, dict):
            return 400, b"invalid JSON", "text/plain"
        try:
            merged = load_routing_params().to_dict()
            merged.update(req)
            params = RoutingParams.from_dict(merged)
        except (ValueError, TypeError) as exc:
            return 400, f"bad routing params: {exc}".encode(), "text/plain"
        save_routing_params(params)
        return 200, json.dumps({"ok": True, "params": params.to_dict()}).encode(), "application/json"

    if path == "/api/routing/jobs":
        model, _, _, _, _, _ = STATE.snapshot
        if model is None:
            return 404, b"no board loaded", "text/plain"
        req = _require_json(body)
        if req is None or not isinstance(req, dict):
            return 400, b"invalid JSON", "text/plain"
        net_names = req.get("net_names", [])
        if not isinstance(net_names, list) or not net_names:
            return 400, b"net_names required (non-empty list)", "text/plain"
        mode_info = STATE.mode_status()
        params_dict = req.get("params", {})
        if isinstance(params_dict, dict) and mode_info["mode"] is not None:
            params_dict = apply_mode(params_dict, mode_info["mode"])
        if not isinstance(params_dict, dict):
            return 400, b"params must be an object", "text/plain"
        try:
            RoutingParams.from_dict(params_dict)
        except (ValueError, TypeError) as exc:
            return 400, f"bad routing params: {exc}".encode(), "text/plain"
        clean_names = [n for n in net_names if isinstance(n, str) and n]
        if not clean_names:
            return 400, b"net_names required (non-empty list)", "text/plain"
        if not _ROUTING_JOB_LOCK.acquire(blocking=False):
            return 409, b"another routing job is running", "text/plain"
        job_id = uuidlib.uuid4().hex[:12]
        mode_info = STATE.mode_status()
        params_dict = req.get("params", {})
        if isinstance(params_dict, dict) and mode_info["mode"] is not None:
            params_dict = apply_mode(params_dict, mode_info["mode"])
        with _JOBS_LOCK:
            _prune_jobs_locked()
            _JOBS[job_id] = {
                "job_id": job_id,
                "state": "queued",
                "net_names": clean_names,
                "params_dict": params_dict,
                "refine": bool(req.get("refine", False)),
                "recovery": bool(req.get("recovery", False)),
                "polish": bool(req.get("polish", True)),
                "mode": mode_info["mode"],
                "mode_warning": mode_info["warning"],
                "cancel_requested": False,
                "created": time.time(),
                "started": None,
                "finished": None,
                "progress": {
                    "stage": "queued",
                    "net": "",
                    "pairs_done": 0,
                    "pairs_total": 0,
                    "expansions": 0,
                    "elapsed_s": 0.0,
                },
                "result": None,
                "error": None,
            }
        worker = threading.Thread(target=_run_routing_job, args=(job_id,), daemon=True)
        try:
            worker.start()
        except Exception:
            with _JOBS_LOCK:
                _JOBS.pop(job_id, None)
            _ROUTING_JOB_LOCK.release()
            raise
        return 200, json.dumps({"ok": True, "job_id": job_id, "state": "queued"}).encode(), "application/json"

    if path.startswith("/api/routing/jobs/") and path.endswith("/cancel"):
        job_id = path[len("/api/routing/jobs/"): -len("/cancel")]
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return 404, b"unknown job", "text/plain"
            job["cancel_requested"] = True
            state = job.get("state")
        return 200, json.dumps({"ok": True, "job_id": job_id, "state": state}).encode(), "application/json"

    if path == "/api/routing/polish":
        return _handle_polish(body)

    if path == "/api/routing/ripup":
        return _handle_ripup(body)

    return None


def _handle_polish(body):
    """Start a background Polish job (re-smooth stored routes, no A*).

    Body: ``{net_names, params}`` — effort is always pinned to 3 (full
    retry ladder); the shared ``smooth_effort`` slider only governs
    route-time smoothing and the automatic end-of-route polish.
    Fast-fails when there is nothing polishable (no stash, board
    changed, unknown nets all protected/missing) — the client treats
    the stale-stash 409s as a scoped re-route fallback.
    """
    req = _require_json(body)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    model, _, _, version, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    net_names = req.get("net_names", [])
    if not isinstance(net_names, list) or not net_names:
        return 400, b"net_names required (non-empty list)", "text/plain"
    params_dict = req.get("params", {})
    if not isinstance(params_dict, dict):
        return 400, b"params must be an object", "text/plain"
    try:
        route_params = RoutingParams.from_dict(params_dict)
    except (ValueError, TypeError) as exc:
        return 400, f"bad routing params: {exc}".encode(), "text/plain"
    # Polish is always full effort: pin the retry ladder regardless of
    # the slider the client sent (0 would be a no-op re-smooth). Stored
    # into the job dict (not just the local) so the worker inherits it.
    route_params.smooth_effort = 3.0
    params_dict["smooth_effort"] = 3.0
    bundle = STATE.get_last_polishable()
    if not bundle.get("routes"):
        return 409, "no routed copper to polish — route first".encode(), "text/plain"
    if bundle.get("version") != version:
        return 409, "board changed since routing — re-route first".encode(), "text/plain"
    if STATE.routing_grid is None:
        return 409, "no routing grid — route first".encode(), "text/plain"
    clean_names = [n for n in net_names if isinstance(n, str) and n]
    if not clean_names:
        return 400, b"net_names required (non-empty list)", "text/plain"
    if not _ROUTING_JOB_LOCK.acquire(blocking=False):
        return 409, b"another routing job is running", "text/plain"
    job_id = uuidlib.uuid4().hex[:12]
    with _JOBS_LOCK:
        _prune_jobs_locked()
        _JOBS[job_id] = {
            "job_id": job_id,
            "state": "queued",
            "net_names": clean_names,
            "params_dict": params_dict,
            "polish": True,
            "cancel_requested": False,
            "created": time.time(),
            "started": None,
            "finished": None,
            "progress": {
                "stage": "queued",
                "net": "",
                "pairs_done": 0,
                "pairs_total": len(clean_names),
                "expansions": 0,
                "elapsed_s": 0.0,
            },
            "result": None,
            "error": None,
        }
    worker = threading.Thread(target=_run_polish_job, args=(job_id,), daemon=True)
    try:
        worker.start()
    except Exception:
        with _JOBS_LOCK:
            _JOBS.pop(job_id, None)
        _ROUTING_JOB_LOCK.release()
        raise
    return 200, json.dumps({"ok": True, "job_id": job_id, "state": "queued"}).encode(), "application/json"


def _handle_ripup(body):
    req = _require_json(body)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    net_names = req.get("net_names", [])
    if not isinstance(net_names, list) or not net_names:
        return 400, b"net_names required (non-empty list)", "text/plain"
    protected_nets = STATE.get_protected_nets()
    requested = [n for n in net_names if isinstance(n, str) and n]
    skipped_protected = sorted({n for n in requested if n in protected_nets})
    rippable = sorted({n for n in requested if n not in protected_nets})
    if not rippable:
        return 200, json.dumps({
            "ok": True, "ripped": {},
            "skipped_protected": skipped_protected,
            "version": STATE.snapshot[3],
        }).encode(), "application/json"
    tracks_before = _Counter(t.net_name for t in model.tracks if t.net_name)
    arcs_before = _Counter(a.net_name for a in model.arcs if a.net_name)
    vias_before = _Counter(v.net_name for v in model.vias if v.net_name)
    STATE.push_undo()
    with STATE._lock:
        STATE.pcb_tree = rip_up_nets(STATE.pcb_tree, set(rippable), protected_nets)
        try:
            STATE.model = board_model(STATE.pcb_tree)
        except Exception:
            pass
        else:
            STATE.routing_model = STATE.model
        STATE.cost_grid = None
        STATE.routing_grid = None
        STATE.version += 1
        version = STATE.version
        new_model = STATE.model
    tracks_after = _Counter(t.net_name for t in new_model.tracks if t.net_name)
    arcs_after = _Counter(a.net_name for a in new_model.arcs if a.net_name)
    vias_after = _Counter(v.net_name for v in new_model.vias if v.net_name)
    ripped = {}
    for net in rippable:
        dt = tracks_before.get(net, 0) - tracks_after.get(net, 0)
        da = arcs_before.get(net, 0) - arcs_after.get(net, 0)
        dv = vias_before.get(net, 0) - vias_after.get(net, 0)
        if dt or da or dv:
            ripped[net] = {"tracks": dt, "arcs": da, "vias": dv}
    payload = {
        "ok": True, "ripped": ripped,
        "skipped_protected": skipped_protected,
        "version": version,
    }
    return 200, json.dumps(payload).encode(), "application/json"
