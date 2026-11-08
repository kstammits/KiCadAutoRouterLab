"""Placement / ghost / footprint HTTP endpoints.

Every status code + body is defined here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

from ui.server import (
    STATE,
    load_params,
    save_params,
    proposal_to_json,
    _movable_uuids_for,
    _footprint_summary,
)

from kicad_autorouter.placement import run_placement
from kicad_autorouter.placement.params import PlacementParams
from kicad_autorouter.svg_render import decap_fp_uuids

logger = logging.getLogger(__name__)


def handle_get(path, qs, handler):
    """GET dispatch for /api/placement/* (plus /api/footprints).

    Returns ``(code, body: bytes, content_type)`` or ``None``.
    """
    if path == "/api/placement/params":
        defaults_only = qs.get("defaults", ["0"])[0] != "0"
        params = PlacementParams() if defaults_only else load_params()
        body = json.dumps(params.to_dict()).encode()
        return 200, body, "application/json"

    if path == "/api/placement/proposal":
        proposal = STATE.current_proposal
        if proposal is None:
            return 404, b"no proposal", "text/plain"
        return 200, json.dumps(proposal_to_json(proposal)).encode(), "application/json"

    if path == "/api/placement/frames":
        proposal = STATE.current_proposal
        if proposal is None:
            body = json.dumps({
                "ok": True, "iterations": 0, "frame_stride": 1,
                "frame_count": 0, "frames": [], "force_frames": [],
            }).encode()
            return 200, body, "application/json"
        frames = getattr(proposal, "frames", None) or []
        force_frames = getattr(proposal, "force_frames", None) or []
        if not frames:
            body = json.dumps({
                "ok": True, "iterations": proposal.iterations,
                "frame_stride": getattr(proposal, "frame_stride", 1),
                "frame_count": 0, "frames": [], "force_frames": force_frames,
            }).encode()
            return 200, body, "application/json"
        body = json.dumps({
            "ok": True, "iterations": proposal.iterations,
            "frame_stride": getattr(proposal, "frame_stride", 1),
            "frame_count": len(frames),
            "frames": proposal_to_json(proposal)["frames"],
            "force_frames": force_frames,
        }).encode()
        return 200, body, "application/json"

    if path == "/api/placement/pinned":
        pinned = STATE.get_pinned_uuids()
        return 200, json.dumps({"ok": True, "pinned": list(pinned)}).encode(), "application/json"

    if path == "/api/placement/selected":
        selected = STATE.get_selected_uuids()
        return 200, json.dumps({"ok": True, "selected": list(selected)}).encode(), "application/json"

    if path == "/api/footprints":
        model, _, _, _, _, _ = STATE.snapshot
        if model is None:
            return 404, b"no board loaded", "text/plain"
        pinned = STATE.get_pinned_uuids()
        selected = STATE.get_selected_uuids()
        locked = {fp.uuid for fp in model.footprints if fp.locked and fp.uuid}
        decaps = decap_fp_uuids(model, STATE.get_power_override_nets())
        footprints_data = []
        for fp in model.footprints:
            if not fp.uuid:
                continue
            if not fp.pads:
                continue
            footprints_data.append(_footprint_summary(fp, pinned, selected, decaps))
        for fp in model.ghost_footprints:
            if not fp.uuid:
                continue
            footprints_data.append(_footprint_summary(
                fp, pinned, selected, decaps, ghost=True))
        return 200, json.dumps({"ok": True, "footprints": footprints_data}).encode(), "application/json"

    return None


def handle_post(path, qs, body, handler):
    """POST dispatch for /api/placement/* plus /api/ghosts/*.

    Returns ``(code, body: bytes, content_type)`` or ``None``.
    """
    if path == "/api/placement/params":
        try:
            incoming = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, b"invalid JSON", "text/plain"
        if not isinstance(incoming, dict):
            return 400, b"params body must be a JSON object", "text/plain"
        merged = load_params().to_dict()
        merged.update(incoming)
        try:
            params = PlacementParams.from_dict(merged)
        except ValueError as exc:
            return 400, f"bad params: {exc}".encode(), "text/plain"
        save_params(params)
        return 200, json.dumps(params.to_dict()).encode(), "application/json"

    if path == "/api/placement/run":
        return _handle_run(path, qs, body, handler)

    if path == "/api/placement/forces":
        return _handle_forces(path, qs, body, handler)

    if path == "/api/placement/accept":
        try:
            version = STATE.accept_proposal()
        except ValueError as exc:
            return 400, f"{exc}".encode(), "text/plain"
        with STATE._lock:
            report = dict(getattr(STATE, "last_accept_report", {}))
        payload = {"ok": True, "version": version}
        payload.update(report)
        return 200, json.dumps(payload).encode(), "application/json"

    if path == "/api/placement/clear":
        version = STATE.set_proposal(None)
        return 200, json.dumps({"ok": True, "version": version}).encode(), "application/json"

    if path == "/api/placement/undo":
        version = STATE.undo()
        return 200, json.dumps({"ok": True, "version": version}).encode(), "application/json"

    if path == "/api/placement/nudge":
        return _handle_nudge(path, qs, body, handler)

    if path == "/api/ghosts":
        return _handle_ghost_create(path, qs, body, handler)

    if path == "/api/ghosts/move":
        return _handle_ghost_move(path, qs, body, handler)

    if path == "/api/ghosts/delete":
        return _handle_ghost_delete(path, qs, body, handler)

    if path == "/api/ghosts/resize":
        return _handle_ghost_resize(path, qs, body, handler)

    if path == "/api/placement/apply":
        return 501, b"writeback not implemented yet", "text/plain"

    if path == "/api/placement/pin":
        return _handle_pin(path, qs, body, handler)

    if path == "/api/placement/select":
        return _handle_select(path, qs, body, handler)

    if path == "/api/placement/selection":
        return _handle_selection(path, qs, body, handler)

    if path == "/api/placement/select_all":
        return _handle_select_all(path, qs, body, handler)

    if path == "/api/placement/clear_selection":
        return _handle_clear_selection(path, qs, body, handler)

    return None


# --------------------------------------------------------------------------- #
# Placement run / refine / forces
# --------------------------------------------------------------------------- #

def _require_json(body, handler):
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _handle_run(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"

    iterations = req.get("iterations")
    movable_uuids = req.get("movable_uuids")
    if isinstance(movable_uuids, list):
        movable_uuids = set(movable_uuids)
    elif movable_uuids is not None:
        return 400, b"movable_uuids must be a list", "text/plain"

    params = load_params()
    if iterations is not None:
        if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 1:
            return 400, b"iterations must be a positive integer", "text/plain"
        params = replace(params, max_iterations=iterations)
    record_frames = bool(req.get("record_frames", False))
    frame_stride = req.get("frame_stride", 1)
    max_frames = req.get("max_frames", 200)
    try:
        frame_stride = max(1, int(frame_stride))
    except (TypeError, ValueError):
        return 400, b"frame_stride must be a positive integer", "text/plain"
    try:
        max_frames = max(1, min(1000, int(max_frames)))
    except (TypeError, ValueError):
        return 400, b"max_frames must be a positive integer", "text/plain"
    if iterations is not None and iterations <= 200:
        record_frames = True

    movable_uuids = _movable_uuids_for(model, STATE, movable_uuids)

    try:
        proposal = run_placement(
            model, params, movable_uuids,
            record_frames=record_frames,
            frame_stride=frame_stride,
            max_frames=max_frames,
            extra_power_nets=STATE.get_power_override_nets(),
        )
    except Exception as exc:
        logger.exception("placement step failed")
        return 500, f"placement failed: {exc}".encode(), "text/plain"
    STATE.set_forces(proposal.forces)
    STATE.set_force_components(proposal.force_components)
    version = STATE.set_proposal(proposal)
    payload = {
        "ok": True, "moved": len(proposal.deltas),
        "iterations": proposal.iterations,
        "final_max_disp_mm": proposal.final_max_disp_mm,
        "elapsed_s": proposal.elapsed_s, "version": version,
        "frame_count": len(getattr(proposal, "frames", None) or []),
        "frame_stride": getattr(proposal, "frame_stride", 1),
        "stop_reason": getattr(proposal, "stop_reason", "budget"),
        "residual_max_force_N": getattr(proposal, "residual_max_force_N", 0.0),
        "depenetrated": getattr(proposal, "depenetrated", False),
        "skipped_uuidless": getattr(proposal, "skipped_uuidless", 0),
        "legality_is_legal": getattr(proposal, "legality_is_legal", True),
        "overlapping_pairs": getattr(proposal, "overlapping_pairs", []),
        "wall_violations": getattr(proposal, "wall_violations", []),
        "max_pair_overlap_mm2": getattr(proposal, "max_pair_overlap_mm2", 0.0),
        "max_wall_penetration_mm": getattr(proposal, "max_wall_penetration_mm", 0.0),
        "exempt_pairs": getattr(proposal, "exempt_pairs", []),
    }
    return 200, json.dumps(payload).encode(), "application/json"


def _handle_forces(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"

    movable_uuids = req.get("movable_uuids")
    if isinstance(movable_uuids, list):
        movable_uuids = set(movable_uuids)
    elif movable_uuids is not None:
        return 400, b"movable_uuids must be a list", "text/plain"

    base_params = load_params()
    merged = base_params.to_dict()
    merged.update({k: v for k, v in req.items() if k != "max_iterations"})
    try:
        params = replace(
            PlacementParams.from_dict(merged), max_iterations=0)
    except (ValueError, TypeError) as exc:
        return 400, f"bad placement params: {exc}".encode(), "text/plain"

    movable_uuids = _movable_uuids_for(model, STATE, movable_uuids)

    try:
        proposal = run_placement(
            model, params, movable_uuids,
            extra_power_nets=STATE.get_power_override_nets(),
        )
    except Exception as exc:
        logger.exception("placement failed")
        return 500, f"placement failed: {exc}".encode(), "text/plain"

    STATE.set_forces(proposal.forces)
    STATE.set_force_components(proposal.force_components)

    payload = {
        "ok": True,
        "forces": {u: [fx, fy] for u, (fx, fy) in proposal.forces.items()},
        "force_components": {
            u: {c: [fx, fy] for c, (fx, fy) in comps.items()}
            for u, comps in proposal.force_components.items()
        },
    }
    return 200, json.dumps(payload).encode(), "application/json"


# --------------------------------------------------------------------------- #
# Nudge / pin / select
# --------------------------------------------------------------------------- #

def _handle_nudge(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    try:
        dx_mm = float(req.get("dx_mm", 0.0))
        dy_mm = float(req.get("dy_mm", 0.0))
        da_deg = float(req.get("da_deg", 0.0))
    except (TypeError, ValueError):
        return 400, b"dx_mm/dy_mm/da_deg must be numbers", "text/plain"
    uuids = req.get("uuids")
    if uuids is None:
        uuids = STATE.get_selected_uuids()
    elif isinstance(uuids, list):
        uuids = set(uuids)
    else:
        return 400, b"uuids must be a list", "text/plain"
    try:
        version, moved = STATE.nudge_footprints(set(uuids), dx_mm, dy_mm, da_deg)
    except ValueError as exc:
        return 400, f"{exc}".encode(), "text/plain"
    return 200, json.dumps({
        "ok": True, "version": version, "moved": moved,
        "dx_mm": dx_mm, "dy_mm": dy_mm, "da_deg": da_deg,
    }).encode(), "application/json"


def _handle_pin(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    uuid = req.get("uuid")
    if not uuid:
        return 400, b"uuid required", "text/plain"
    sel_version = STATE.toggle_pinned(uuid)
    pinned = STATE.get_pinned_uuids()
    _, _, _, version, _, _ = STATE.snapshot
    return 200, json.dumps({
        "ok": True, "pinned": list(pinned), "version": version,
        "selection_version": sel_version,
    }).encode(), "application/json"


def _handle_select(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    uuid = req.get("uuid")
    if not uuid:
        return 400, b"uuid required", "text/plain"
    explicit = req.get("selected")
    sel_version = STATE.toggle_selected(
        uuid, selected=bool(explicit) if isinstance(explicit, bool) else None)
    selected = STATE.get_selected_uuids()
    _, _, _, version, _, _ = STATE.snapshot
    return 200, json.dumps({
        "ok": True, "selected": list(selected), "version": version,
        "selection_version": sel_version,
    }).encode(), "application/json"


def _handle_selection(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    uuids = req.get("selected", req.get("uuids", []))
    if not isinstance(uuids, list):
        return 400, b"selected must be a list", "text/plain"
    sel_version = STATE.replace_selection(set(uuids))
    selected = STATE.get_selected_uuids()
    _, _, _, version, _, _ = STATE.snapshot
    return 200, json.dumps({
        "ok": True, "selected": list(selected), "version": version,
        "selection_version": sel_version,
    }).encode(), "application/json"


def _handle_select_all(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    sel_version = STATE.select_all_unlocked(model)
    selected = STATE.get_selected_uuids()
    _, _, _, version, _, _ = STATE.snapshot
    return 200, json.dumps({
        "ok": True, "selected": list(selected), "version": version,
        "selection_version": sel_version,
    }).encode(), "application/json"


def _handle_clear_selection(path, qs, body, handler):
    sel_version = STATE.clear_selection()
    _, _, _, version, _, _ = STATE.snapshot
    return 200, json.dumps({
        "ok": True, "version": version, "selection_version": sel_version,
    }).encode(), "application/json"


# --------------------------------------------------------------------------- #
# Ghosts
# --------------------------------------------------------------------------- #

def _handle_ghost_create(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    try:
        version, uuid = STATE.ghost_create(
            kind=req.get("kind", "wall"),
            ref=req.get("ref", ""),
            x_mm=float(req.get("x_mm", 0.0)),
            y_mm=float(req.get("y_mm", 0.0)),
            w_mm=req.get("w_mm"),
            h_mm=req.get("h_mm"),
            target_ref=req.get("target_ref"),
            ka=req.get("ka"),
            size_mm=req.get("size_mm", 4.0),
            courtyard_layer=req.get("courtyard_layer", "F.CrtYd"),
        )
    except (ValueError, TypeError) as exc:
        return 400, f"{exc}".encode(), "text/plain"
    return 200, json.dumps({"ok": True, "version": version, "uuid": uuid}).encode(), "application/json"


def _handle_ghost_move(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    if not req.get("uuid"):
        return 400, b"uuid required", "text/plain"
    try:
        version = STATE.ghost_move(
            req["uuid"],
            float(req.get("dx_mm", 0.0)),
            float(req.get("dy_mm", 0.0)),
            float(req.get("da_deg", 0.0)),
        )
    except (ValueError, TypeError) as exc:
        return 400, f"{exc}".encode(), "text/plain"
    return 200, json.dumps({"ok": True, "version": version}).encode(), "application/json"


def _handle_ghost_delete(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    if not req.get("uuid"):
        return 400, b"uuid required", "text/plain"
    try:
        version = STATE.ghost_delete(req["uuid"])
    except ValueError as exc:
        return 400, f"{exc}".encode(), "text/plain"
    return 200, json.dumps({"ok": True, "version": version}).encode(), "application/json"


def _handle_ghost_resize(path, qs, body, handler):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    req = _require_json(body, handler)
    if req is None or not isinstance(req, dict):
        return 400, b"invalid JSON", "text/plain"
    if not req.get("uuid"):
        return 400, b"uuid required", "text/plain"
    if req.get("w_mm") is None or req.get("h_mm") is None:
        return 400, b"w_mm and h_mm required", "text/plain"
    try:
        version = STATE.ghost_resize(req["uuid"], req["w_mm"], req["h_mm"])
    except ValueError as exc:
        return 400, f"{exc}".encode(), "text/plain"
    return 200, json.dumps({"ok": True, "version": version}).encode(), "application/json"
