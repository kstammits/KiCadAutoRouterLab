import json
import logging
import signal
import sys
import threading
import time
import tomllib
import uuid as uuidlib
from dataclasses import replace
from typing import Dict

logger = logging.getLogger(__name__)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from kicad_autorouter.board_model import (
    board_model,
    copper_layers as tree_copper_layers,
    is_ground_net,
    is_power_net_effective,
)
from kicad_autorouter.io import (
    collapse_to_two_layer,
    rip_up_nets,
)
from kicad_autorouter.mass import courtyard_bbox_area_m2
from kicad_autorouter.placement import (
    PlacementParams,
    PlacementProposal,
    check_legality,
    refine_placement,
    refine_placement_with_report,
    run_placement,
)
from kicad_autorouter.placement.model import (
    collect_pad_nodes,
    find_decap_chip_links,
)
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    terminals_for_net,
    resolve_clearance,
    resolve_track_width,
    route_nets,
)
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.obstacles import build_fanout_cost
from kicad_autorouter.routing.impedance import (
    BRIDGE_DEFAULT_MOHM,
    DEFAULT_GND_MAX_MOHM,
    DEFAULT_POWER_MAX_MOHM,
    InletSpec,
    analyze_power_gnd,
    suggest_header_mates,
)
from kicad_autorouter.routing.pour import gnd_islands
from kicad_autorouter.routing.power import identify_power_nets
from kicad_autorouter.strategy import (
    MODES,
    MODE_PRESETS,
    apply_mode,
    mode_owned_keys,
)
from kicad_autorouter.svg_render import decap_fp_uuids, net_colors, render_board_svg

from ui.debug_grids import _cost_grid_to_debug, _secondary_to_debug

# App version from pyproject.toml
with open(REPO_ROOT / "pyproject.toml", "rb") as f:
    APP_VERSION = tomllib.load(f)["project"]["version"]

# User-tuned placement parameters persist here (gitignored).
PARAMS_PATH = REPO_ROOT / "placement.json"

# MIME types for the UI_DIR static fallback below. Stylesheets are
# MIME-blocked by browsers unless served as text/css, and scripts need
# text/javascript; everything else keeps the previous octet-stream default.
_STATIC_MIME = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}

# Curated docs servable via /api/docs/*. Large KiCad manuals and wikipedia
# sources are deliberately excluded (pcbnew.md alone is ~700KB and would
# freeze the side panel on inject).
DOCS_ALLOWLIST: Dict[str, Path] = {
    "README.md": REPO_ROOT / "README.md",
    "docs/README.md": REPO_ROOT / "docs" / "README.md",
    "docs/autorouting_glossary.md": REPO_ROOT / "docs" / "autorouting_glossary.md",
    "docs/si_force_laws.md": REPO_ROOT / "docs" / "si_force_laws.md",
    "docs/physics_primer.qmd": REPO_ROOT / "docs" / "physics_primer.qmd",
    "docs/roadmap.md": REPO_ROOT / "docs" / "roadmap.md",
}
DOCS_MAX_BYTES = 200 * 1024

# Pre-rendered Quarto HTML (regenerated on demand via scripts/regen_*.sh,
# gitignored under docs/_output/). Served verbatim so figures + math render;
# never rendered on HTTP request.
# Maps URL basename -> (output file, source file for staleness, or None).
DOCS_RENDERED: Dict[str, tuple[Path, Path | None]] = {
    "physics_primer.html": (
        REPO_ROOT / "docs" / "_output" / "html" / "physics_primer.html",
        REPO_ROOT / "docs" / "physics_primer.qmd",
    ),
}
# Reverse map: allowlist source name -> rendered URL basename.
DOCS_SOURCE_TO_RENDERED: Dict[str, str] = {
    "docs/physics_primer.qmd": "physics_primer.html",
}


def _docs_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
    return fallback


def _safe_filename(name: str, suffix: str) -> str:
    """Board-download filename that cannot break out of its header.

    ``STATE.name`` arrives verbatim from ``/api/load?name=``; strip path
    components and any quote/CR/LF (response splitting) plus other
    RFC 6266-unsafe characters, keeping ``[A-Za-z0-9._-]``. Always ends
    with ``suffix`` (a ``.kicad_pcb`` name).
    """
    stem = (name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.endswith(".kicad_pcb"):
        stem = stem[: -len(".kicad_pcb")]
    stem = "".join(c for c in stem if c.isalnum() or c in "._-").strip("._") or "board"
    return stem[:128] + suffix


def _docs_entry(name: str, path: Path) -> dict:
    try:
        size = path.stat().st_size
        source_mtime = path.stat().st_mtime
    except OSError:
        size = 0
        source_mtime = 0.0
    title = name
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(2048)
        title = _docs_title(head, name)
    except OSError:
        pass
    entry: dict = {"name": name, "title": title, "size_bytes": size}
    out = DOCS_SOURCE_TO_RENDERED.get(name)
    if out is not None:
        rendered_path, _ = DOCS_RENDERED[out]
        try:
            rendered_mtime = rendered_path.stat().st_mtime
        except OSError:
            rendered_mtime = None
        entry["rendered_url"] = f"/docs-rendered/{out}" if rendered_mtime is not None else None
        entry["rendered_stale"] = rendered_mtime is not None and rendered_mtime < source_mtime
    else:
        entry["rendered_url"] = None
        entry["rendered_stale"] = False
    return entry


from ui.server_state import NUDGE_COALESCE_S, STATE
from ui.server_jobs import (
    _JOBS,
    _JOBS_LOCK,
    _ROUTING_JOB_LOCK,
    _drc_lock,
    _prune_jobs_locked,
    _run_drc_now,
    _run_routing_job,
)


def _read_params_file() -> dict:
    """placement.json as a dict; missing/corrupt/non-dict reads as {}."""
    if PARAMS_PATH.is_file():
        try:
            data = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
        except ValueError:
            pass  # corrupt file falls back to defaults; next save overwrites it
        else:
            if isinstance(data, dict):
                return data
    return {}


def _write_params_file(data: dict) -> None:
    PARAMS_PATH.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def load_params() -> PlacementParams:
    """Effective params: defaults overlaid with placement.json when present."""
    try:
        return PlacementParams.from_dict(_read_params_file())
    except ValueError:
        return PlacementParams()


def load_routing_params() -> RoutingParams:
    """Load routing params from placement.json routing section."""
    try:
        return RoutingParams.from_dict(_read_params_file().get("routing", {}))
    except ValueError:
        return RoutingParams()


def save_params(params: PlacementParams) -> None:
    # Load existing data to preserve routing section
    existing = _read_params_file()
    existing.update(params.to_dict())
    _write_params_file(existing)


def save_routing_params(params: RoutingParams) -> None:
    # Load existing data to preserve placement section
    existing = _read_params_file()
    existing["routing"] = params.to_dict()
    _write_params_file(existing)



def proposal_to_json(proposal, include_frames=True) -> dict:
    def r5(x):
        return round(x, 5) if isinstance(x, float) else x

    def r3(x):
        return round(x, 3) if isinstance(x, float) else x

    def r2(x):
        return round(x, 2) if isinstance(x, float) else x

    out = {
        "deltas": {u: [r5(dx), r5(dy), r5(da)] for u, (dx, dy, da) in proposal.deltas.items()},
        "iterations": proposal.iterations,
        "final_max_disp_mm": r5(proposal.final_max_disp_mm),
        "elapsed_s": r5(proposal.elapsed_s),
        "params": proposal.params,
        "stop_reason": getattr(proposal, "stop_reason", "budget"),
        "residual_max_force_N": r5(getattr(proposal, "residual_max_force_N", 0.0)),
        "depenetrated": getattr(proposal, "depenetrated", False),
        "skipped_uuidless": getattr(proposal, "skipped_uuidless", 0),
        "legality_is_legal": getattr(proposal, "legality_is_legal", True),
        "overlapping_pairs": getattr(proposal, "overlapping_pairs", []),
        "wall_violations": getattr(proposal, "wall_violations", []),
        "max_pair_overlap_mm2": r5(getattr(proposal, "max_pair_overlap_mm2", 0.0)),
        "max_wall_penetration_mm": r5(getattr(proposal, "max_wall_penetration_mm", 0.0)),
        "exempt_pairs": getattr(proposal, "exempt_pairs", []),
        "degenerate_bodies": list(getattr(proposal, "degenerate_bodies", ())),
    }
    frames = getattr(proposal, "frames", None) or []
    if include_frames and frames:
        out["frames"] = [
            {u: [r3(dx), r3(dy), r2(da)] for u, (dx, dy, da) in fr.items()}
            for fr in frames
        ]
        out["frame_stride"] = getattr(proposal, "frame_stride", 1)
        out["frame_count"] = len(frames)
    force_frames = getattr(proposal, "force_frames", None) or []
    if include_frames and force_frames:
        out["force_frames"] = force_frames
    return out


def _movable_uuids_for(model, state, req_uuids):
    """Resolve the movable footprint set for a placement request.

    ``req_uuids`` is the client-supplied list (or ``None``). When ``None``,
    derive all unlocked, non-pinned footprints. When provided, drop any pinned
    UUIDs. Returns a set of UUIDs.
    """
    if req_uuids is None:
        return {
            fp.uuid for fp in model.footprints
            if fp.uuid and not fp.locked and fp.uuid not in state.get_pinned_uuids()
        }
    pinned = state.get_pinned_uuids()
    return {u for u in req_uuids if u not in pinned}


def _footprint_summary(fp, pinned, selected, decaps, ghost=False):
    """Build one /api/footprints entry, shared by the real and ghost loops.

    Field order matches the historical hand-built dicts byte-for-byte.
    """
    nets = sorted({pad.net_name for pad in fp.pads if pad.net_name})
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for a, b in fp.courtyard:
        minx = min(minx, a.x_mm, b.x_mm)
        miny = min(miny, a.y_mm, b.y_mm)
        maxx = max(maxx, a.x_mm, b.x_mm)
        maxy = max(maxy, a.y_mm, b.y_mm)
    if minx == float("inf"):
        minx = miny = maxx = maxy = 0.0
    pad_count = sum(1 for pad in fp.pads if pad.net_name is not None)
    out = {
        "uuid": fp.uuid,
        "ref": fp.ref,
        "footprint_id": fp.footprint_id,
        "layer": fp.layer,
        "x_mm": fp.x_mm,
        "y_mm": fp.y_mm,
        "angle_deg": fp.angle_deg,
        "locked": fp.locked if not ghost else False,
        "pinned": (fp.uuid in pinned) if not ghost else False,
        "selected": (fp.uuid in selected) if not ghost else False,
        "nets": nets,
        "bbox": [minx, miny, maxx, maxy],
        "ghost": ghost,
    }
    if ghost:
        out["ghost_attractions"] = [[t, ka] for t, ka in (fp.ghost_attractions or ())]
    out["is_decap"] = (fp.uuid in decaps) if not ghost else False
    out["pad_count"] = pad_count
    return out


def _routing_result_payload(result) -> dict:
    """Shared routing-result body core (ok through partial_arcs).

    Endpoint-specific fields (version, mode_warning, candidates, debug) are
    added by the caller so JSON field order stays byte-identical to before.
    """
    return {
        "ok": True,
        "tracks_added": result.tracks_added,
        "arcs_added": result.arcs_added,
        "vias_added": result.vias_added,
        "fanout_vias_added": result.fanout_vias_added,
        "fanout_tracks_added": result.fanout_tracks_added,
        "nets_routed": result.nets_routed,
        "nets_failed": result.nets_failed,
        "drc_clean": result.drc_clean,
        "drc_violations": result.drc_violations,
        "stitch_vias_added": result.stitch_vias_added,
        "stitch_tracks_added": result.stitch_tracks_added,
        "gnd_islands_before": result.gnd_islands_before,
        "gnd_islands_after": result.gnd_islands_after,
        "gnd_stripped": result.gnd_stripped,
        "cancelled": result.cancelled,
        "expansions": result.expansions,
        "timings": result.timings,
        "failed_reasons": result.failed_reasons,
        "partial_tracks": [
            {"start": list(t.start), "end": list(t.end),
             "width_mm": t.width_mm, "layer": t.layer,
             "net_name": t.net_name}
            for t in result.partial_tracks
        ],
        "partial_vias": [
            {"position": list(v.position), "size_mm": v.size_mm,
             "drill_mm": v.drill_mm, "layers": list(v.layers),
             "net_name": v.net_name}
            for v in result.partial_vias
        ],
        "partial_arcs": [
            {"start": list(a.start), "mid": list(a.mid), "end": list(a.end),
             "width_mm": a.width_mm, "layer": a.layer,
             "net_name": a.net_name}
            for a in result.partial_arcs
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/api/version":
            self._send(200, json.dumps({"version": APP_VERSION}).encode(), "application/json")
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/docs/list":
            docs = [_docs_entry(name, DOCS_ALLOWLIST[name]) for name in sorted(DOCS_ALLOWLIST)]
            self._send(200, json.dumps({"ok": True, "docs": docs}).encode(), "application/json")
            return
        if parsed.path == "/api/docs/content":
            name = (qs.get("name") or [""])[0]
            if name not in DOCS_ALLOWLIST:
                self._send(404, b"unknown doc", "text/plain")
                return
            full = DOCS_ALLOWLIST[name].resolve()
            if not str(full).startswith(str(REPO_ROOT.resolve())) or not full.is_file():
                self._send(404, b"unknown doc", "text/plain")
                return
            try:
                text = full.read_text(encoding="utf-8")
            except OSError:
                self._send(404, b"unknown doc", "text/plain")
                return
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
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/state":
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
            self._send(200, body.encode(), "application/json")
            return
        if parsed.path == "/api/drc/status":
            self._send(200, json.dumps(STATE.get_drc_status()).encode(), "application/json")
            return
        if parsed.path == "/api/mode":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            mode_info = STATE.mode_status()
            payload = {"ok": True, **mode_info}
            if mode_info["mode"] is not None:
                payload["preset"] = MODE_PRESETS[mode_info["mode"]]
                payload["owned"] = mode_owned_keys(mode_info["mode"])
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/board/collapse-preview":
            tree = STATE.get_pcb_tree()
            if tree is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            _, report = collapse_to_two_layer(tree)
            self._send(200, json.dumps({"ok": True, **STATE.mode_status(),
                                        "report": report}).encode(),
                       "application/json")
            return
        if parsed.path == "/api/board.svg":
            model, _, name, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            overlay_on = qs.get("overlay", ["1"])[0] != "0"
            forces_on = qs.get("forces", ["0"])[0] != "0"
            drc_on = qs.get("drc", ["0"])[0] != "0"
            # Layer visibility is client-side (applyLayerVisibility) so all
            # copper layers stay in the SVG; fcu/bcu/l_* params are accepted
            # and ignored for backward compatibility with old clients.
            fills_on = qs.get("fills", ["0"])[0] != "0"
            proposal = STATE.current_proposal if overlay_on else None
            pinned = STATE.get_pinned_uuids()
            selected = STATE.get_selected_uuids()
            
            # Create a minimal proposal for forces display if needed
            if forces_on and proposal is None:
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
                # Merge stored forces into existing proposal
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
            
            # DRC overlay: draw cached violations; dim them when the cache is
            # for an older board version or a fresh run is in flight.
            drc_violations = None
            drc_stale = False
            if drc_on:
                status = STATE.get_drc_status()
                if status["for_version"] is not None:
                    drc_violations = STATE.get_drc_violations()
                    drc_stale = status["stale"] or status["running"]

            # Include force markers when motion frames exist so the client
            # can draw force vectors client-side during frame scrubbing.
            # Read from STATE (not the overlay-gated `proposal` above, which
            # is None when the scrub path requests overlay=0&forces=0).
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
            self._send(200, svg, "image/svg+xml")
            return
        if parsed.path == "/api/placement/params":
            defaults_only = qs.get("defaults", ["0"])[0] != "0"
            params = PlacementParams() if defaults_only else load_params()
            self._send(200, json.dumps(params.to_dict()).encode(), "application/json")
            return
        if parsed.path == "/api/placement/proposal":
            proposal = STATE.current_proposal
            if proposal is None:
                self._send(404, b"no proposal", "text/plain")
                return
            body = json.dumps(proposal_to_json(proposal)).encode()
            self._send(200, body, "application/json")
            return
        if parsed.path == "/api/placement/frames":
            proposal = STATE.current_proposal
            if proposal is None:
                # Poll-friendly empty payload (not a 404): the client polls
                # this on every status tick, and a missing proposal simply
                # means no motion is loaded yet.
                body = json.dumps({
                    "ok": True,
                    "iterations": 0,
                    "frame_stride": 1,
                    "frame_count": 0,
                    "frames": [],
                    "force_frames": [],
                }).encode()
                self._send(200, body, "application/json")
                return
            frames = getattr(proposal, "frames", None) or []
            force_frames = getattr(proposal, "force_frames", None) or []
            if not frames:
                body = json.dumps({
                    "ok": True,
                    "iterations": proposal.iterations,
                    "frame_stride": getattr(proposal, "frame_stride", 1),
                    "frame_count": 0,
                    "frames": [],
                    "force_frames": force_frames,
                }).encode()
                self._send(200, body, "application/json")
                return
            body = json.dumps({
                "ok": True,
                "iterations": proposal.iterations,
                "frame_stride": getattr(proposal, "frame_stride", 1),
                "frame_count": len(frames),
                "frames": proposal_to_json(proposal)["frames"],
                "force_frames": force_frames,
            }).encode()
            self._send(200, body, "application/json")
            return
        if parsed.path == "/api/placement/pinned":
            pinned = STATE.get_pinned_uuids()
            self._send(200, json.dumps({"ok": True, "pinned": list(pinned)}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/selected":
            selected = STATE.get_selected_uuids()
            self._send(200, json.dumps({"ok": True, "selected": list(selected)}).encode(), "application/json")
            return
        if parsed.path == "/api/board/download":
            tree = STATE.get_pcb_tree()
            if tree is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            from kicad_autorouter.sexpr import to_sexpr
            mode_info = STATE.mode_status()
            layers = tree_copper_layers(tree)
            # Two-layer mode with inner copper present: serve a collapsed
            # copy (never mutates the in-memory board). Anything else —
            # including four-layer mode on a 2Cu board — serves as-is with
            # at most a warning, never a block.
            if mode_info["mode"] == "two-layer" and any(
                l not in ("F.Cu", "B.Cu") for l in layers
            ):
                tree, _ = collapse_to_two_layer(tree)
                suffix = "-2layer.kicad_pcb"
            else:
                suffix = "-modified.kicad_pcb"
            filename = _safe_filename(STATE.name, suffix)
            content = to_sexpr(tree) + "\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(content.encode())))
            self.end_headers()
            self.wfile.write(content.encode())
            return
        if parsed.path == "/api/footprints":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            pinned = STATE.get_pinned_uuids()
            selected = STATE.get_selected_uuids()
            locked = {fp.uuid for fp in model.footprints if fp.locked and fp.uuid}
            # Footprints special-treated as decoupling caps (C*, 2 pads,
            # power+ground) — same rule as placement + svg_render halo.
            decaps = decap_fp_uuids(model, STATE.get_power_override_nets())
            footprints_data = []
            # Real footprints (padless graphical parts are hidden from the
            # UI for framerate: no pads, no physics, not sent — they stay
            # in the model/tree so writeback is unaffected; ghosts below
            # are unaffected).
            for fp in model.footprints:
                if not fp.uuid:
                    continue
                if not fp.pads:
                    continue
                footprints_data.append(_footprint_summary(fp, pinned, selected, decaps))
            # Ghost footprints
            for fp in model.ghost_footprints:
                if not fp.uuid:
                    continue
                footprints_data.append(_footprint_summary(fp, pinned, selected, decaps, ghost=True))
            self._send(200, json.dumps({"ok": True, "footprints": footprints_data}).encode(), "application/json")
            return
        
        if parsed.path == "/api/nets":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            protected = STATE.get_protected_nets()
            extra_power = STATE.get_power_override_nets()
            rp = load_routing_params()
            # Count existing tracks/arcs/vias per net (arcs are separate:
            # the nets table shows both columns).
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
            # net -> decap refs using the same C*/power+ground rule as
            # placement + svg_render (computed from real footprints;
            # user-marked power counts as power). decap_links maps
            # decap ref -> nearest U* chip ref (None when unassigned).
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
            # Sort: protected first, then by pad count descending
            nets_data.sort(key=lambda n: (not n["protected"], -n["pad_count"]))
            self._send(200, json.dumps(
                {"ok": True, "nets": nets_data, "decap_links": decap_links}
            ).encode(), "application/json")
            return

        # GET /api/nets/terminals?net=<name> - pad coordinates of one
        # net's terminals (board mm). Pure model data: no routing grid
        # needed, so it works before routing and after. Powers the
        # nets-table select-to-zoom (client unions across selected nets).
        if parsed.path == "/api/nets/terminals":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            net_name = (qs.get("net", [""])[0] or "")
            if net_name not in model.nets:
                self._send(404, f"unknown net: {net_name}".encode(), "text/plain")
                return
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
            self._send(200, json.dumps({"ok": True, "net": net_name,
                                        "count": len(terms),
                                        "multi_region": multi,
                                        "terminals": terms}).encode(),
                       "application/json")
            return

        # GET /api/nets/inlets - inlet anchors for impedance screening.
        if parsed.path == "/api/nets/inlets":
            inlets = STATE.get_inlets()
            self._send(200, json.dumps({"ok": True, **inlets}).encode(), "application/json")
            return

        # GET /api/nets/bridge-suggestions - candidate inter-panel header
        # mates (same position, same pin count). Same-net pairs are ready
        # bridge inputs; different-net pairs need user adjudication.
        if parsed.path == "/api/nets/bridge-suggestions":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            try:
                mates = suggest_header_mates(model)
            except Exception as exc:
                self._send(500, f"suggestion failed: {exc}".encode(), "text/plain")
                return
            self._send(200, json.dumps({"ok": True, "mates": mates}).encode(), "application/json")
            return
        
        # GET /api/routing/params
        if parsed.path == "/api/routing/params":
            params = load_routing_params()
            self._send(200, json.dumps({"ok": True, "params": params.to_dict()}).encode(), "application/json")
            return

        # GET /api/routing/debug/cost-grid - return cost grid for visualization.
        # ?net=<name> also builds that net's per-net secondary (fanout halo)
        # banded for overlay + its terminal cells (own-cell value/sealed flag).
        if parsed.path == "/api/routing/debug/cost-grid":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            if STATE.cost_grid is None or STATE.routing_grid is None:
                self._send(404, b"no cost grid available (run routing first)", "text/plain")
                return
            payload = _cost_grid_to_debug(STATE.cost_grid, STATE.routing_grid)
            net_name = (qs.get("net", [""])[0] or "")
            if net_name:
                if net_name not in model.nets:
                    self._send(404, f"unknown net: {net_name}".encode(), "text/plain")
                    return
                try:
                    params = load_routing_params()
                    width = resolve_track_width(model, params, net_name, ())
                    clear = resolve_clearance(model, params, net_name)
                    secondary = build_fanout_cost(
                        model, STATE.routing_grid, net_name,
                        clearance_mm=clear, track_half_mm=width / 2.0)
                    terminals = terminals_for_net(model, STATE.routing_grid, net_name)
                except Exception as exc:
                    self._send(500, f"secondary build failed: {exc}".encode(), "text/plain")
                    return
                payload["net"] = net_name
                payload["secondary"] = _secondary_to_debug(
                    secondary, STATE.routing_grid, terminals)
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        
        # GET /api/routing/debug/gnd-islands - pour continuity diagnostics.
        # No routing run required: builds a 0.2mm grid on the fly (4x fewer
        # cells than the 0.1mm routing grid; topology equivalent for viz).
        if parsed.path == "/api/routing/debug/gnd-islands":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            try:
                grid = create_grid_from_model(model, resolution_mm=0.2)
                islands = gnd_islands(model, grid)
            except Exception as exc:
                self._send(500, f"island analysis failed: {exc}".encode(), "text/plain")
                return
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
            self._send(200, json.dumps(payload).encode(), "application/json")
            return

        # GET /api/routing/debug/impedance - inlet-anchored DC-resistance
        # screening for GND + power nets (report only, no routing change).
        # Query: gnd_max_mohm, power_max_mohm, nets (comma list filter),
        # include_all_power=1 (also evaluate keyword power rails lacking an inlet).
        if parsed.path == "/api/routing/debug/impedance":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            try:
                q = parse_qs(parsed.query)
                def _qfloat(name: str, default: float) -> float:
                    try:
                        return float((q.get(name) or [default])[0])
                    except (TypeError, ValueError):
                        return default
                gnd_max = _qfloat("gnd_max_mohm", DEFAULT_GND_MAX_MOHM)
                power_max = _qfloat("power_max_mohm", DEFAULT_POWER_MAX_MOHM)
                nets_filter = None
                if q.get("nets"):
                    nets_filter = [n for n in (q.get("nets") or [""])[0].split(",") if n]
                inlets = STATE.get_inlets()
                gnd_inlets = [InletSpec(**a) for a in (inlets.get("gnd_list") or [])]
                power_inlets = {n: [InletSpec(**a) for a in (lst or [])]
                                for n, lst in (inlets.get("power") or {}).items()}
                extra_power = STATE.get_power_override_nets()
                _power, _ground, _signal = identify_power_nets(model, extra_power)
                gnd_nets = [n for n in model.nets if is_ground_net(n)]
                if nets_filter is not None:
                    gnd_nets = [n for n in gnd_nets if n in nets_filter]
                if str((q.get("include_all_power") or [""])[0]).lower() in ("1", "true", "yes"):
                    for n in _power:
                        power_inlets.setdefault(n, [])
                power_nets = [n for n, lst in power_inlets.items() if lst]
                if nets_filter is not None:
                    power_nets = [n for n in power_nets if n in nets_filter]
                auto = str((q.get("auto_bridges") or ["1"])[0]).lower() not in ("0", "false", "no")
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
                self._send(200, json.dumps(report.to_dict()).encode(), "application/json")
            except Exception as exc:
                self._send(500, f"impedance analysis failed: {exc}".encode(), "text/plain")
            return

        # GET /api/routing/jobs/<job_id> - poll an async routing job.
        if parsed.path.startswith("/api/routing/jobs/"):
            job_id = parsed.path[len("/api/routing/jobs/"):]
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                snapshot = dict(job) if job else None
                if snapshot is not None:
                    snapshot["progress"] = dict(job.get("progress") or {})
                    if job.get("result") is not None:
                        snapshot["result"] = job["result"]
            if snapshot is None:
                self._send(404, b"unknown job", "text/plain")
                return
            self._send(200, json.dumps(snapshot).encode(), "application/json")
            return

        # GET /docs-rendered/<name>.html - pre-rendered Quarto HTML (see DOCS_RENDERED).
        if parsed.path.startswith("/docs-rendered/"):
            base = parsed.path[len("/docs-rendered/"):]
            if base not in DOCS_RENDERED or "/" in base:
                self._send(404, b"unknown doc", "text/plain")
                return
            out_path, _ = DOCS_RENDERED[base]
            full = out_path.resolve()
            if not str(full).startswith(str(REPO_ROOT.resolve())) or not full.is_file():
                self._send(404, b"unknown doc", "text/plain")
                return
            self._send(200, full.read_bytes(), "text/html")
            return

        rel_path = parsed.path.lstrip("/")
        rel = Path(rel_path) if rel_path else Path("index.html")
        full = (UI_DIR / rel).resolve()
        if not str(full).startswith(str(UI_DIR.resolve())):
            self._send(403, b"forbidden", "text/plain")
            return
        if not full.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = _STATIC_MIME.get(full.suffix, "application/octet-stream")
        body = full.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Never cache the UI: a restarted server with a cached page is a
        # half-old client against a new server (or vice versa), which
        # shows up as dead buttons and mystery ReferenceErrors.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/load":
            kind = "sch" if (qs.get("kind") or ["pcb"])[0] == "sch" else "pcb"
            name = (qs.get("name") or ["board.kicad_pcb"])[0]
            path_q = (qs.get("path") or [None])[0]
            if path_q:
                full = (REPO_ROOT / path_q).resolve()
                if not full.is_relative_to(REPO_ROOT.resolve()) or not full.is_file():
                    self._send(400, b"bad path", "text/plain")
                    return
                data = full.read_bytes()
            else:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._send(400, b"empty body", "text/plain")
                    return
                if length > 32 * 1024 * 1024:
                    self._send(400, b"board too large", "text/plain")
                    return
                data = self.rfile.read(length)
            try:
                version = STATE.load(data, name, kind)
            except Exception as exc:
                self._send(400, f"parse error: {exc}".encode(), "text/plain")
                return
            payload = {
                "ok": True,
                "name": name,
                "kind": kind,
                "version": version,
                "board_version": STATE.board_version,
                "warning": STATE.version_warning,
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/drc":
            # Explicit DRC run. Single-flight: concurrent callers block on the
            # lock and then serve the fresh cache instead of spawning another
            # kicad-cli process. A previous error never counts as fresh — an
            # explicit click always retries.
            with _drc_lock:
                tree, current_version = STATE.snapshot_pcb()
                if tree is None:
                    self._send(404, b"no board loaded", "text/plain")
                    return
                status = STATE.get_drc_status()
                if (
                    not status["running"]
                    and status["for_version"] == current_version
                    and status["error"] is None
                ):
                    by_type: Dict[str, int] = {}
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
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/placement/params":
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length > 0 else b""
            try:
                incoming = json.loads(data.decode("utf-8"))
                if not isinstance(incoming, dict):
                    raise ValueError("params body must be a JSON object")
                # Partial-update merge: overlay incoming keys onto the saved
                # params so GUI-hidden knobs (power/decap/ghost/solver
                # internals) survive a POST that only carries visible sliders.
                merged = load_params().to_dict()
                merged.update(incoming)
                params = PlacementParams.from_dict(merged)
            except ValueError as exc:
                self._send(400, f"bad params: {exc}".encode(), "text/plain")
                return
            save_params(params)
            self._send(200, json.dumps(params.to_dict()).encode(), "application/json")
            return
        if parsed.path == "/api/placement/run":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            # Read JSON body for optional parameters
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return

            iterations = req.get("iterations")
            movable_uuids = req.get("movable_uuids")
            if isinstance(movable_uuids, list):
                movable_uuids = set(movable_uuids)
            elif movable_uuids is not None:
                self._send(400, b"movable_uuids must be a list", "text/plain")
                return

            params = load_params()
            if iterations is not None:
                if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 1:
                    self._send(400, b"iterations must be a positive integer", "text/plain")
                    return
                # Step budget overrides the saved max_iterations; other settings preserved.
                params = replace(params, max_iterations=iterations)
            # Trajectory frames for the motion player: explicit opt-in, plus
            # auto-record on short Step budgets (<=200 iters) where per-iter
            # Kabsch capture is cheap and strange-moves debugging happens.
            record_frames = bool(req.get("record_frames", False))
            frame_stride = req.get("frame_stride", 1)
            max_frames = req.get("max_frames", 200)
            try:
                frame_stride = max(1, int(frame_stride))
            except (TypeError, ValueError):
                self._send(400, b"frame_stride must be a positive integer", "text/plain")
                return
            try:
                max_frames = max(1, min(1000, int(max_frames)))
            except (TypeError, ValueError):
                self._send(400, b"max_frames must be a positive integer", "text/plain")
                return
            if iterations is not None and iterations <= 200:
                record_frames = True

# If movable_uuids not provided, compute as all unlocked except pinned
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
                self._send(500, f"placement failed: {exc}".encode(), "text/plain")
                return
            STATE.set_forces(proposal.forces)
            STATE.set_force_components(proposal.force_components)
            version = STATE.set_proposal(proposal)
            payload = {
                "ok": True,
                "moved": len(proposal.deltas),
                "iterations": proposal.iterations,
                "final_max_disp_mm": proposal.final_max_disp_mm,
                "elapsed_s": proposal.elapsed_s,
                "version": version,
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
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/placement/refine":
            import time as _time
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            if not isinstance(req, dict):
                self._send(400, b"params must be an object", "text/plain")
                return
            movable_uuids = req.get("movable_uuids")
            if isinstance(movable_uuids, list):
                movable_uuids = set(movable_uuids)
            elif movable_uuids is not None:
                self._send(400, b"movable_uuids must be a list", "text/plain")
                return
            saved = load_params()
            strength = req.get("strength", saved.refine_strength)
            radius_m = req.get("radius_m", saved.refine_radius_m)
            try:
                strength = float(strength)
                radius_m = float(radius_m)
            except (TypeError, ValueError):
                self._send(400, b"strength/radius_m must be numbers", "text/plain")
                return
            if not (0.0 <= strength <= 1.0):
                self._send(400, b"strength must be in [0.0, 1.0]", "text/plain")
                return
            if not radius_m > 0:
                self._send(400, b"radius_m must be > 0", "text/plain")
                return
            if movable_uuids is None:
                movable_uuids = {
                    fp.uuid for fp in model.footprints
                    if fp.uuid and not fp.locked and fp.uuid not in STATE.get_pinned_uuids()
                }
            else:
                pinned = STATE.get_pinned_uuids()
                movable_uuids = {u for u in movable_uuids if u not in pinned}
            base = STATE.current_proposal
            base_deltas = dict(base.deltas) if base is not None else {}
            start = _time.perf_counter()
            try:
                extra, tidy_report = refine_placement_with_report(
                    model, base_deltas,
                    strength=strength,
                    radius_mm=radius_m * 1000.0,
                    movable_uuids=movable_uuids,
                    overlap_tolerance_mm2=saved.overlap_tolerance_mm2,
                    wall_tolerance_mm=saved.wall_tolerance_mm,
                )
            except Exception as exc:
                logger.exception("refine failed")
                self._send(500, f"refine failed: {exc}".encode(), "text/plain")
                return
            merged = dict(base_deltas)
            merged.update(extra)
            legality = check_legality(
                model, merged,
                overlap_tolerance_mm2=saved.overlap_tolerance_mm2,
                wall_tolerance_mm=saved.wall_tolerance_mm,
            )
            params_dict = saved.to_dict()
            params_dict["refine_strength"] = strength
            params_dict["refine_radius_m"] = radius_m
            proposal = PlacementProposal(
                deltas=merged,
                iterations=getattr(base, "iterations", 0) if base is not None else 0,
                final_max_disp_mm=max(
                    ((float(v[0]) ** 2 + float(v[1]) ** 2) ** 0.5 for v in merged.values()),
                    default=0.0,
                ),
                elapsed_s=_time.perf_counter() - start,
                params=params_dict,
                forces=dict(getattr(base, "forces", {}) or {}),
                force_components=dict(getattr(base, "force_components", {}) or {}),
                stop_reason="refined",
                residual_max_force_N=float(getattr(base, "residual_max_force_N", 0.0) or 0.0),
                legality_is_legal=legality.is_legal,
                overlapping_pairs=legality.overlapping_pairs,
                wall_violations=legality.wall_violations,
                max_pair_overlap_mm2=legality.max_pair_overlap_mm2,
                max_wall_penetration_mm=legality.max_wall_penetration_mm,
                exempt_pairs=legality.exempt_pairs,
            )
            version = STATE.set_proposal(proposal)
            payload = {
                "ok": True,
                "moved": len(merged),
                "refined": len(extra),
                "version": version,
                "stop_reason": "refined",
                "tidy_report": tidy_report,
                "legality_is_legal": legality.is_legal,
                "overlapping_pairs": list(legality.overlapping_pairs),
                "wall_violations": list(legality.wall_violations),
                "max_pair_overlap_mm2": legality.max_pair_overlap_mm2,
                "max_wall_penetration_mm": legality.max_wall_penetration_mm,
                "exempt_pairs": list(legality.exempt_pairs),
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/placement/forces":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            # Read JSON body for optional parameters
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return

            movable_uuids = req.get("movable_uuids")
            if isinstance(movable_uuids, list):
                movable_uuids = set(movable_uuids)
            elif movable_uuids is not None:
                self._send(400, b"movable_uuids must be a list", "text/plain")
                return

            # Use parameters from request if provided, else load from file.
            # SI v2 set (newtons, meters, seconds); max_iterations=0 makes
            # this a static force probe. Merged dict goes through from_dict
            # so client values face the same validation as /api/placement/params.
            base_params = load_params()
            if not isinstance(req, dict):
                self._send(400, b"params must be an object", "text/plain")
                return
            merged = base_params.to_dict()
            merged.update({k: v for k, v in req.items()
                           if k != "max_iterations"})
            try:
                params = replace(
                    PlacementParams.from_dict(merged), max_iterations=0)
            except (ValueError, TypeError) as exc:
                self._send(400, f"bad placement params: {exc}".encode(), "text/plain")
                return

            # If movable_uuids not provided, compute as all unlocked except pinned
            movable_uuids = _movable_uuids_for(model, STATE, movable_uuids)

            try:
                proposal = run_placement(
                    model, params, movable_uuids,
                    extra_power_nets=STATE.get_power_override_nets(),
                )
            except Exception as exc:
                logger.exception("placement failed")
                self._send(500, f"placement failed: {exc}".encode(), "text/plain")
                return

            # Store forces in BoardState for SVG rendering
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
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/placement/accept":
            try:
                version = STATE.accept_proposal()
            except ValueError as exc:
                self._send(400, f"{exc}".encode(), "text/plain")
                return
            with STATE._lock:
                report = dict(getattr(STATE, "last_accept_report", {}))
            payload = {"ok": True, "version": version}
            payload.update(report)
            self._send(
                200,
                json.dumps(payload).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/placement/clear":
            version = STATE.set_proposal(None)
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/placement/undo":
            version = STATE.undo()
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/placement/nudge":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            try:
                dx_mm = float(req.get("dx_mm", 0.0))
                dy_mm = float(req.get("dy_mm", 0.0))
                da_deg = float(req.get("da_deg", 0.0))
            except (TypeError, ValueError):
                self._send(400, b"dx_mm/dy_mm/da_deg must be numbers", "text/plain")
                return
            uuids = req.get("uuids")
            if uuids is None:
                uuids = STATE.get_selected_uuids()
            elif isinstance(uuids, list):
                uuids = set(uuids)
            else:
                self._send(400, b"uuids must be a list", "text/plain")
                return
            try:
                version, moved = STATE.nudge_footprints(
                    set(uuids), dx_mm, dy_mm, da_deg
                )
            except ValueError as exc:
                self._send(400, f"{exc}".encode(), "text/plain")
                return
            self._send(
                200,
                json.dumps({
                    "ok": True, "version": version, "moved": moved,
                    "dx_mm": dx_mm, "dy_mm": dy_mm, "da_deg": da_deg,
                }).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/ghosts" and self.command == "POST":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
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
                self._send(400, f"{exc}".encode(), "text/plain")
                return
            self._send(
                200,
                json.dumps({"ok": True, "version": version, "uuid": uuid}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/ghosts/move":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            if not req.get("uuid"):
                self._send(400, b"uuid required", "text/plain")
                return
            try:
                version = STATE.ghost_move(
                    req["uuid"],
                    float(req.get("dx_mm", 0.0)),
                    float(req.get("dy_mm", 0.0)),
                    float(req.get("da_deg", 0.0)),
                )
            except (ValueError, TypeError) as exc:
                self._send(400, f"{exc}".encode(), "text/plain")
                return
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/ghosts/delete":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            if not req.get("uuid"):
                self._send(400, b"uuid required", "text/plain")
                return
            try:
                version = STATE.ghost_delete(req["uuid"])
            except ValueError as exc:
                self._send(400, f"{exc}".encode(), "text/plain")
                return
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/ghosts/resize":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            if not req.get("uuid"):
                self._send(400, b"uuid required", "text/plain")
                return
            if req.get("w_mm") is None or req.get("h_mm") is None:
                self._send(400, b"w_mm and h_mm required", "text/plain")
                return
            try:
                version = STATE.ghost_resize(req["uuid"], req["w_mm"], req["h_mm"])
            except ValueError as exc:
                self._send(400, f"{exc}".encode(), "text/plain")
                return
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/board/reset":
            version = STATE.reset_to_original()
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/placement/apply":
            self._send(501, b"writeback not implemented yet", "text/plain")
            return
        if parsed.path == "/api/placement/pin":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            uuid = req.get("uuid")
            if not uuid:
                self._send(400, b"uuid required", "text/plain")
                return
            sel_version = STATE.toggle_pinned(uuid)
            pinned = STATE.get_pinned_uuids()
            _, _, _, version, _, _ = STATE.snapshot
            self._send(200, json.dumps({"ok": True, "pinned": list(pinned), "version": version, "selection_version": sel_version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/select":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            uuid = req.get("uuid")
            if not uuid:
                self._send(400, b"uuid required", "text/plain")
                return
            # Idempotent when the client sends its optimistic state;
            # legacy toggle when omitted.
            explicit = req.get("selected")
            sel_version = STATE.toggle_selected(
                uuid, selected=bool(explicit) if isinstance(explicit, bool) else None)
            selected = STATE.get_selected_uuids()
            _, _, _, version, _, _ = STATE.snapshot
            self._send(200, json.dumps({"ok": True, "selected": list(selected), "version": version, "selection_version": sel_version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/selection":
            # Bulk replace the whole selection set in one round-trip
            # (single-select, select-visible, clear). Unknown/locked uuids
            # are ignored server-side for robustness.
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            uuids = req.get("selected", req.get("uuids", []))
            if not isinstance(uuids, list):
                self._send(400, b"selected must be a list", "text/plain")
                return
            sel_version = STATE.replace_selection(set(uuids))
            selected = STATE.get_selected_uuids()
            _, _, _, version, _, _ = STATE.snapshot
            self._send(200, json.dumps({"ok": True, "selected": list(selected), "version": version, "selection_version": sel_version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/select_all":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            sel_version = STATE.select_all_unlocked(model)
            selected = STATE.get_selected_uuids()
            _, _, _, version, _, _ = STATE.snapshot
            self._send(200, json.dumps({"ok": True, "selected": list(selected), "version": version, "selection_version": sel_version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/clear_selection":
            sel_version = STATE.clear_selection()
            _, _, _, version, _, _ = STATE.snapshot
            self._send(200, json.dumps({"ok": True, "version": version, "selection_version": sel_version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/pinned":
            pinned = STATE.get_pinned_uuids()
            self._send(200, json.dumps({"ok": True, "pinned": list(pinned)}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/selected":
            selected = STATE.get_selected_uuids()
            self._send(200, json.dumps({"ok": True, "selected": list(selected)}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/apply":
            self._send(501, b"writeback not implemented yet", "text/plain")
            return
        
        # ==================== Nets API ====================
        
        if parsed.path == "/api/nets/protected" and self.command == "GET":
            protected = STATE.get_protected_nets()
            self._send(200, json.dumps({"ok": True, "protected": sorted(protected)}).encode(), "application/json")
            return
        
        # POST /api/nets/protected - explicitly mark/unmark a protected net.
        # (`protected: true` toggles instead
        # of setting — unchecking a stale-checked box could unprotect.)
        if parsed.path == "/api/nets/protected" and self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            net_name = req.get("net")
            protected_flag = req.get("protected")
            if not net_name:
                self._send(400, b"net name required", "text/plain")
                return
            with STATE._lock:
                if protected_flag:
                    STATE.protected_nets.add(net_name)
                else:
                    STATE.protected_nets.discard(net_name)
                version = STATE.version + 1
                STATE.version = version
            protected = STATE.get_protected_nets()
            self._send(200, json.dumps({"ok": True, "protected": sorted(protected), "version": version}).encode(), "application/json")
            return

        # GET /api/nets/power - user-marked power membership
        if parsed.path == "/api/nets/power" and self.command == "GET":
            extra = STATE.get_power_override_nets()
            self._send(200, json.dumps({"ok": True, "power_override": sorted(extra)}).encode(), "application/json")
            return

        # POST /api/nets/power - explicitly mark/unmark a net as power.
        # Marked nets route first with wide tracks, join protection, and
        # count for placement magnetism + decap detection. Explicit-set
        # semantics both ways (no toggle quirk).
        if parsed.path == "/api/nets/power" and self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            net_name = req.get("net")
            if not net_name or "power" not in req:
                self._send(400, b"net name and power flag required", "text/plain")
                return
            version = STATE.set_power_override_net(net_name, bool(req.get("power")))
            extra = STATE.get_power_override_nets()
            self._send(200, json.dumps({"ok": True, "power_override": sorted(extra), "version": version}).encode(), "application/json")
            return

        # POST /api/nets/inlets - set/clear inlet anchors for impedance screening.
        # Body: {"gnd": {"ref","pad"}|[{...}]|null, "gnd_add": {...},
        #        "gnd_clear": true, "power": {net: {"ref","pad"}|[{...}]|null}}.
        # Single objects replace that net's list (null clears); lists replace
        # wholesale. One GND anchor per panel region; pads are measured from
        # the anchor in their own region. Refs/pads are validated against
        # the loaded board (unknown ref/pad/net -> 400).
        if parsed.path == "/api/nets/inlets":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return

            def _check_anchor(anchor):
                # Pad numbers may be empty strings (mounting holes), so test
                # presence, not truthiness, for "pad".
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
                        self._send(400, b"gnd must be {ref, pad}, a list, or null", "text/plain")
                        return
                    STATE.set_gnd_inlets(checked)
            if "gnd_add" in req:
                ok = _check_anchor(req.get("gnd_add"))
                if ok is None:
                    self._send(400, b"gnd_add must be {ref, pad}", "text/plain")
                    return
                STATE.add_gnd_inlet(ok["ref"], ok["pad"])
            if "power" in req:
                p = req.get("power")
                if not isinstance(p, dict):
                    self._send(400, b"power must be an object", "text/plain")
                    return
                for net_name, anchor in p.items():
                    if net_name not in model.nets:
                        self._send(400, f"unknown net {net_name}".encode(), "text/plain")
                        return
                    if anchor is None:
                        STATE.set_power_inlets(net_name, [])
                    else:
                        checked = _check_list(anchor)
                        if checked is None:
                            self._send(400, f"bad inlet anchor for {net_name}".encode(), "text/plain")
                            return
                        STATE.set_power_inlets(net_name, checked)
            if "bridges" in req:
                b = req.get("bridges")
                if b is None:
                    STATE.set_bridges([])
                elif not isinstance(b, list):
                    self._send(400, b"bridges must be a list or null", "text/plain")
                    return
                else:
                    checked_bridges = []
                    for item in b:
                        if not (isinstance(item, dict) and isinstance(item.get("a"), dict)
                                and isinstance(item.get("b"), dict)):
                            self._send(400, b"bridge must be {a: {ref,pad}, b: {ref,pad}}", "text/plain")
                            return
                        a_ok = _check_anchor(item["a"])
                        b_ok = _check_anchor(item["b"])
                        if a_ok is None or b_ok is None:
                            self._send(400, b"unknown bridge ref/pad", "text/plain")
                            return
                        try:
                            r = float(item.get("r_mohm", BRIDGE_DEFAULT_MOHM))
                        except (TypeError, ValueError):
                            r = BRIDGE_DEFAULT_MOHM
                        checked_bridges.append({"a": a_ok, "b": b_ok, "r_mohm": max(r, 0.0)})
                    STATE.set_bridges(checked_bridges)
            inlets = STATE.get_inlets()
            self._send(200, json.dumps({"ok": True, **inlets, "version": STATE.version}).encode(), "application/json")
            return
        
        # ==================== Routing API ====================

        # POST /api/mode {mode} - switch the per-board routing mode.
        # Validates + stores; the preset applies as an in-memory overlay at
        # job-submit time (never written to placement.json, which is global
        # while mode is per-board). Returns the applied preset for the toast.
        if parsed.path == "/api/mode":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            mode = req.get("mode")
            if mode not in MODES:
                self._send(400, f"mode must be one of {list(MODES)}".encode(),
                           "text/plain")
                return
            STATE.set_mode(mode)
            self._send(200, json.dumps({"ok": True, **STATE.mode_status(),
                                        "preset": MODE_PRESETS[mode],
                                        "owned": mode_owned_keys(mode)}).encode(),
                       "application/json")
            return

        # POST /api/routing/params - save routing params
        # (Note: inside do_POST, self.command is always "POST", so no method check needed.
        # GET /api/routing/params lives in do_GET.)
        if parsed.path == "/api/routing/params":
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            if not isinstance(req, dict):
                self._send(400, b"params must be an object", "text/plain")
                return
            try:
                # Partial-update merge (same as placement): overlay incoming
                # keys onto the saved routing params so API-only knobs
                # (hidden UI sliders, script-tuned budgets) survive a POST
                # that only carries visible inputs.
                merged = load_routing_params().to_dict()
                merged.update(req)
                params = RoutingParams.from_dict(merged)
            except (ValueError, TypeError) as exc:
                self._send(400, f"bad routing params: {exc}".encode(), "text/plain")
                return
            save_routing_params(params)
            self._send(200, json.dumps({"ok": True, "params": params.to_dict()}).encode(), "application/json")
            return
        
        # POST /api/routing/jobs - start an async routing job (one net per
        # job from the chunked UI). Returns {job_id} immediately; poll via
        # GET /api/routing/jobs/<id>. Only one routing job runs at a time
        # (409 busy otherwise) because jobs share the routing grids.
        if parsed.path == "/api/routing/jobs":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            net_names = req.get("net_names", [])
            if not isinstance(net_names, list) or not net_names:
                self._send(400, b"net_names required (non-empty list)", "text/plain")
                return
            mode_info = STATE.mode_status()
            params_dict = req.get("params", {})
            if isinstance(params_dict, dict) and mode_info["mode"] is not None:
                params_dict = apply_mode(params_dict, mode_info["mode"])
            if not isinstance(params_dict, dict):
                self._send(400, b"params must be an object", "text/plain")
                return
            try:
                RoutingParams.from_dict(params_dict)
            except (ValueError, TypeError) as exc:
                self._send(400, f"bad routing params: {exc}".encode(), "text/plain")
                return
            clean_names = [n for n in net_names if isinstance(n, str) and n]
            if not clean_names:
                self._send(400, b"net_names required (non-empty list)", "text/plain")
                return
            if not _ROUTING_JOB_LOCK.acquire(blocking=False):
                self._send(409, b"another routing job is running", "text/plain")
                return
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
            self._send(200, json.dumps({"ok": True, "job_id": job_id, "state": "queued"}).encode(), "application/json")
            return

        # POST /api/routing/jobs/<job_id>/cancel - cooperative cancel. The
        # worker stops between nets/attempts/pairs (partial routes are kept);
        # poll the job to observe completion.
        if parsed.path.startswith("/api/routing/jobs/") and parsed.path.endswith("/cancel"):
            job_id = parsed.path[len("/api/routing/jobs/"): -len("/cancel")]
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is None:
                    self._send(404, b"unknown job", "text/plain")
                    return
                job["cancel_requested"] = True
                state = job.get("state")
            self._send(200, json.dumps({"ok": True, "job_id": job_id, "state": state}).encode(), "application/json")
            return

        # POST /api/routing/run - route selected nets
        if parsed.path == "/api/routing/run":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            net_names = req.get("net_names", [])
            if not isinstance(net_names, list) or not net_names:
                self._send(400, b"net_names required (non-empty list)", "text/plain")
                return
            # Load routing params from request or file, overlaid with the
            # current board-mode preset (in-memory only — never written back).
            route_params_dict = req.get("params", {})
            mode_info = STATE.mode_status()
            if isinstance(route_params_dict, dict) and mode_info["mode"] is not None:
                route_params_dict = apply_mode(route_params_dict, mode_info["mode"])
            if not isinstance(route_params_dict, dict):
                self._send(400, b"params must be an object", "text/plain")
                return
            try:
                route_params = RoutingParams.from_dict(route_params_dict)
            except (ValueError, TypeError) as exc:
                self._send(400, f"bad routing params: {exc}".encode(), "text/plain")
                return
            protected_nets = STATE.get_protected_nets()
            
            try:
                result, cost_grid, routing_grid = route_nets(
                    model=model,
                    net_names=net_names,
                    protected_nets=protected_nets,
                    cost_grid=STATE.cost_grid,
                    routing_grid=STATE.routing_grid,
                    params=route_params,
                    original_pcb_tree=STATE.pcb_tree,
                    extra_power_nets=STATE.get_power_override_nets(),
                )
            except ValueError as exc:
                # Client error (e.g. grid too large for the board): 400 with
                # an actionable message, not a 500.
                self._send(400, f"routing rejected: {exc}".encode(), "text/plain")
                return
            except Exception as exc:
                logger.exception("routing failed")
                self._send(500, f"routing failed: {exc}".encode(), "text/plain")
                return
            
            # Update BoardState with new routing state.
            # NOTE: /api/board.svg and /api/nets render from STATE.model, so the
            # model must gain the new copper — updating only pcb_tree leaves the
            # UI showing zero new tracks. Rebuild the model from the updated tree
            # (footprints are untouched by routing, so a rebuild is equivalent
            # plus the new tracks/vias).
            with STATE._lock:
                STATE.cost_grid = cost_grid
                STATE.routing_grid = routing_grid
                if result.pcb_tree is not None:
                    STATE.pcb_tree = result.pcb_tree
                    try:
                        STATE.model = board_model(result.pcb_tree)
                    except Exception:
                        # Tree writeback succeeded but re-parse failed; keep the
                        # old model rather than dropping the board.
                        STATE.routing_model = model
                    else:
                        STATE.routing_model = STATE.model
                else:
                    STATE.routing_model = model
                STATE.version += 1
                version = STATE.version
            
            payload = _routing_result_payload(result)
            payload["mode_warning"] = mode_info["warning"]
            payload["version"] = version
            # Include debug info if requested (sync /api/routing/run)
            # Include debug info if requested
            if route_params.debug:
                payload["debug"] = _cost_grid_to_debug(cost_grid, routing_grid)
            self._send(200, json.dumps(payload).encode(), "application/json")
            return

        # POST /api/routing/ripup - strip copper for selected nets, no re-route.
        # Explicit per-net rip-up: removes (segment)/(via) nodes for the
        # requested nets while leaving everything else (incl. protected nets)
        # untouched. Pushes undo first so the existing Undo button restores.
        if parsed.path == "/api/routing/ripup":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            net_names = req.get("net_names", [])
            if not isinstance(net_names, list) or not net_names:
                self._send(400, b"net_names required (non-empty list)", "text/plain")
                return
            protected_nets = STATE.get_protected_nets()
            requested = [n for n in net_names if isinstance(n, str) and n]
            skipped_protected = sorted({n for n in requested if n in protected_nets})
            rippable = sorted({n for n in requested if n not in protected_nets})
            if not rippable:
                self._send(
                    200,
                    json.dumps({
                        "ok": True,
                        "ripped": {},
                        "skipped_protected": skipped_protected,
                        "version": STATE.snapshot[3],
                    }).encode(),
                    "application/json",
                )
                return
            from collections import Counter as _Counter
            tracks_before = _Counter(t.net_name for t in model.tracks if t.net_name)
            arcs_before = _Counter(a.net_name for a in model.arcs if a.net_name)
            vias_before = _Counter(v.net_name for v in model.vias if v.net_name)
            STATE.push_undo()
            with STATE._lock:
                STATE.pcb_tree = rip_up_nets(STATE.pcb_tree, set(rippable), protected_nets)
                try:
                    STATE.model = board_model(STATE.pcb_tree)
                except Exception:
                    # Keep the old model rather than dropping the board;
                    # undo stack still holds the pre-rip state.
                    pass
                else:
                    STATE.routing_model = STATE.model
                # Copper changed -> cached occupancy grids are stale.
                STATE.cost_grid = None
                STATE.routing_grid = None
                STATE.version += 1
                version = STATE.version
                new_model = STATE.model
            tracks_after = _Counter(t.net_name for t in new_model.tracks if t.net_name)
            arcs_after = _Counter(a.net_name for a in new_model.arcs if a.net_name)
            vias_after = _Counter(v.net_name for v in new_model.vias if v.net_name)
            ripped: dict = {}
            for net in rippable:
                dt = tracks_before.get(net, 0) - tracks_after.get(net, 0)
                da = arcs_before.get(net, 0) - arcs_after.get(net, 0)
                dv = vias_before.get(net, 0) - vias_after.get(net, 0)
                if dt or da or dv:
                    ripped[net] = {"tracks": dt, "arcs": da, "vias": dv}
            payload = {
                "ok": True,
                "ripped": ripped,
                "skipped_protected": skipped_protected,
                "version": version,
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return

        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # keep console quiet; capturable via logging
        logger.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Workflow UI: http://127.0.0.1:{port}", flush=True)

    # Idempotent: Ctrl-C raises SIGINT in the shell wrapper too, which used
    # to forward a second SIGTERM mid-shutdown and print a traceback. The
    # guard makes repeats a no-op; shutdown() runs off-thread because it
    # blocks until serve_forever() returns.
    _shutting_down = threading.Event()

    def shutdown(signum, frame):
        if _shutting_down.is_set():
            return
        _shutting_down.set()
        print("\nShutting down...", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Fallback if a SIGINT arrives before the handler is installed or
        # from an untrapped source; the handler path above already prints.
        shutdown(signal.SIGINT, None)
        # serve_forever() returns via shutdown(); block briefly so the
        # message above isn't lost on interpreter teardown.
        _shutting_down.wait(timeout=5.0)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
