"""Board / docs / mode / static HTTP endpoints.

Extracted from ui/server.py (Step 3: split god modules). Behavior-neutral move
only; every status code + body is unchanged.
"""

from __future__ import annotations

import json
import logging
import tempfile
import tomllib
from pathlib import Path

from ui.server import (
    APP_VERSION,
    DOCS_ALLOWLIST,
    DOCS_MAX_BYTES,
    DOCS_RENDERED,
    DOCS_SOURCE_TO_RENDERED,
    REPO_ROOT,
    STATE,
    UI_DIR,
    _STATIC_MIME,
    _docs_entry,
    _docs_title,
    _safe_filename,
)

from kicad_autorouter.board_model import copper_layers as tree_copper_layers
from kicad_autorouter.io import collapse_to_two_layer
from kicad_autorouter.sexpr import parse_file, to_sexpr
from kicad_autorouter.svg_render import render_board_svg
from kicad_autorouter.validate import refill_zones
from kicad_autorouter.strategy import (
    MODES,
    MODE_PRESETS,
    apply_mode,
    mode_owned_keys,
)

from ui.server_jobs import _drc_lock, _run_drc_now

logger = logging.getLogger(__name__)

# `tempfile`, `tomllib`, `parse_file`, `refill_zones` are used by /api/drc
# and /api/board.svg keep-alive paths; keep imports for signature parity.


def handle_get(path, qs, handler):
    """GET dispatch for /api/version, /api/docs/*, /api/state, /api/mode,
    /api/board/*, /docs-rendered/*, and the static fallback.

    Returns ``(code, body: bytes, content_type)`` or ``None``.
    """
    if path == "/api/version":
        return 200, json.dumps({"version": APP_VERSION}).encode(), "application/json"

    if path == "/api/docs/list":
        docs = [_docs_entry(name, DOCS_ALLOWLIST[name]) for name in sorted(DOCS_ALLOWLIST)]
        return 200, json.dumps({"ok": True, "docs": docs}).encode(), "application/json"

    if path == "/api/docs/content":
        name = (qs.get("name") or [""])[0]
        if name not in DOCS_ALLOWLIST:
            return 404, b"unknown doc", "text/plain"
        full = DOCS_ALLOWLIST[name].resolve()
        if not str(full).startswith(str(REPO_ROOT.resolve())) or not full.is_file():
            return 404, b"unknown doc", "text/plain"
        try:
            text = full.read_text(encoding="utf-8")
        except OSError:
            return 404, b"unknown doc", "text/plain"
        truncated = False
        if len(text.encode("utf-8")) > DOCS_MAX_BYTES:
            text = text[:DOCS_MAX_BYTES]
            truncated = True
        payload = {
            "ok": True,
            "name": name,
            "title": _docs_title(text, name),
            "markdown": text,
            "truncated": truncated,
            **{k: v for k, v in _docs_entry(name, full).items() if k in ("rendered_url", "rendered_stale")},
        }
        return 200, json.dumps(payload).encode(), "application/json"

    if path == "/api/state":
        model, has_sch, name, version, board_version, version_warning = STATE.snapshot
        mode_info = STATE.mode_status()
        body = json.dumps(
            {
                "loaded": model is not None,
                "name": name,
                "version": version,
                "selection_version": STATE.get_selection_version(),
                "has_sch": has_sch,
                "board_version": board_version,
                "warning": version_warning,
                "copper_layers": list(model.copper_layers) if model is not None else [],
                "mode": mode_info["mode"],
                "mode_warning": mode_info["warning"],
            }
        )
        return 200, body.encode(), "application/json"

    if path == "/api/mode":
        model, _, _, _, _, _ = STATE.snapshot
        if model is None:
            return 404, b"no board loaded", "text/plain"
        mode_info = STATE.mode_status()
        payload = {"ok": True, **mode_info}
        if mode_info["mode"] is not None:
            payload["preset"] = MODE_PRESETS[mode_info["mode"]]
            payload["owned"] = mode_owned_keys(mode_info["mode"])
        return 200, json.dumps(payload).encode(), "application/json"

    if path == "/api/board/collapse-preview":
        tree = STATE.get_pcb_tree()
        if tree is None:
            return 404, b"no board loaded", "text/plain"
        _, report = collapse_to_two_layer(tree)
        return 200, json.dumps({"ok": True, **STATE.mode_status(),
                                "report": report}).encode(), "application/json"

    if path == "/api/board.svg":
        return _handle_board_svg(qs)

    if path == "/api/board/download":
        return _handle_board_download()

    if path.startswith("/docs-rendered/"):
        base = path[len("/docs-rendered/"):]
        if base not in DOCS_RENDERED or "/" in base:
            return 404, b"unknown doc", "text/plain"
        out_path, _ = DOCS_RENDERED[base]
        full = out_path.resolve()
        if not str(full).startswith(str(REPO_ROOT.resolve())) or not full.is_file():
            return 404, b"unknown doc", "text/plain"
        return 200, full.read_bytes(), "text/html"

    return None


def _handle_board_svg(qs):
    model, _, name, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    overlay_on = qs.get("overlay", ["1"])[0] != "0"
    forces_on = qs.get("forces", ["0"])[0] != "0"
    drc_on = qs.get("drc", ["0"])[0] != "0"
    fills_on = qs.get("fills", ["0"])[0] != "0"
    proposal = STATE.current_proposal if overlay_on else None
    pinned = STATE.get_pinned_uuids()
    selected = STATE.get_selected_uuids()

    if forces_on and proposal is None:
        from kicad_autorouter.placement import PlacementProposal
        proposal = PlacementProposal(
            deltas={},
            iterations=0,
            final_max_disp_mm=0.0,
            elapsed_s=0.0,
            params={},
            forces=STATE.get_forces(),
            force_components=STATE.get_force_components(),
        )
    elif forces_on and proposal is not None:
        stored_forces = STATE.get_forces()
        stored_comps = STATE.get_force_components()
        if stored_forces or stored_comps:
            merged_forces = {**proposal.forces, **stored_forces}
            merged_comps = {**proposal.force_components, **stored_comps}
            proposal = PlacementProposal(
                deltas=proposal.deltas,
                iterations=proposal.iterations,
                final_max_disp_mm=proposal.final_max_disp_mm,
                elapsed_s=proposal.elapsed_s,
                params=proposal.params,
                forces=merged_forces,
                force_components=merged_comps,
                stop_reason=getattr(proposal, "stop_reason", "budget"),
                residual_max_force_N=getattr(proposal, "residual_max_force_N", 0.0),
                depenetrated=getattr(proposal, "depenetrated", False),
                skipped_uuidless=getattr(proposal, "skipped_uuidless", 0),
            )

    drc_violations = None
    drc_stale = False
    if drc_on:
        status = STATE.get_drc_status()
        if status["for_version"] is not None:
            drc_violations = STATE.get_drc_violations()
            drc_stale = status["stale"] or status["running"]

    _stored = STATE.current_proposal
    _frames = getattr(_stored, "force_frames", None) or []
    include_force_markers = len(_frames) > 0

    svg = render_board_svg(model, title=name, proposal=proposal,
                           pinned_uuids=pinned, selected_uuids=selected,
                           show_forces=forces_on,
                           drc_violations=drc_violations,
                           drc_stale=drc_stale,
                           include_force_markers=include_force_markers,
                           extra_power_nets=STATE.get_power_override_nets(),
                           show_fills=fills_on).encode()
    return 200, svg, "image/svg+xml"


def _handle_board_download():
    tree = STATE.get_pcb_tree()
    if tree is None:
        return 404, b"no board loaded", "text/plain"
    mode_info = STATE.mode_status()
    layers = tree_copper_layers(tree)
    if mode_info["mode"] == "two-layer" and any(
        l not in ("F.Cu", "B.Cu") for l in layers
    ):
        tree, _ = collapse_to_two_layer(tree)
        suffix = "-2layer.kicad_pcb"
    else:
        suffix = "-modified.kicad_pcb"
    filename = _safe_filename(STATE.name, suffix)
    content = to_sexpr(tree) + "\n"
    # 4-tuple: (code, body, content_type, extra_headers). server.py's _send
    # accepts an optional headers dict.
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return 200, content.encode(), "application/octet-stream", headers


def handle_post(path, qs, body, handler):
    """POST dispatch for /api/load, /api/drc, /api/board/reset, /api/mode.

    Returns ``(code, body: bytes, content_type)`` or ``None``.
    """
    if path == "/api/load":
        return _handle_load(body, qs, handler)

    if path == "/api/drc":
        return _handle_drc()

    if path == "/api/board/reset":
        return _handle_board_reset()

    if path == "/api/mode":
        return _handle_mode_post(body)

    return None


def _handle_load(body, qs, handler):
    kind = "sch" if (qs.get("kind") or ["pcb"])[0] == "sch" else "pcb"
    name = (qs.get("name") or ["board.kicad_pcb"])[0]
    path_q = (qs.get("path") or [None])[0]
    if path_q:
        full = (REPO_ROOT / path_q).resolve()
        if not full.is_relative_to(REPO_ROOT.resolve()) or not full.is_file():
            return 400, b"bad path", "text/plain"
        data = full.read_bytes()
    else:
        if len(body) <= 0:
            return 400, b"empty body", "text/plain"
        if len(body) > 32 * 1024 * 1024:
            return 400, b"board too large", "text/plain"
        data = body
    try:
        version = STATE.load(data, name, kind)
    except Exception as exc:
        return 400, f"parse error: {exc}".encode(), "text/plain"
    payload = {
        "ok": True,
        "name": name,
        "kind": kind,
        "version": version,
        "board_version": STATE.board_version,
        "warning": STATE.version_warning,
    }
    return 200, json.dumps(payload).encode(), "application/json"


def _handle_drc():
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
                "ok": True,
                "cached": True,
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


def _handle_board_reset():
    version = STATE.reset_to_original()
    return 200, json.dumps({"ok": True, "version": version}).encode(), "application/json"


def _handle_mode_post(body):
    model, _, _, _, _, _ = STATE.snapshot
    if model is None:
        return 404, b"no board loaded", "text/plain"
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, b"invalid JSON", "text/plain"
    mode = req.get("mode")
    if mode not in MODES:
        return 400, f"mode must be one of {list(MODES)}".encode(), "text/plain"
    STATE.set_mode(mode)
    return (
        200,
        json.dumps({"ok": True, **STATE.mode_status(),
                    "preset": MODE_PRESETS[mode],
                    "owned": mode_owned_keys(mode)}).encode(),
        "application/json",
    )
