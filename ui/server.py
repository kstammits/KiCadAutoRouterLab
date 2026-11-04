"""Workflow UI HTTP server: dispatch + shared state.

Extracted from ui/server.py (Step 3: split god modules). Behavior-neutral move
only: this module keeps the public names tests and scripts rely on
(``Handler``, ``STATE``, ``PARAMS_PATH``, ``load_params``/``save_params``,
``load_routing_params``/``save_routing_params``, ``proposal_to_json``,
``main``) and the :class:`Handler` class, whose ``do_GET``/``do_POST`` now
delegate to the domain modules in :mod:`ui.handlers`.

Handler modules import shared state/helpers from ``ui.server``. To avoid
importing a second copy of this module (tests load it as the top-level
``server`` via ``ui/`` on ``sys.path``), ``ui.server`` is aliased to this very
module object at import time so all shared state (notably ``PARAMS_PATH``) is
single-instance.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import tomllib
from typing import Dict

logger = logging.getLogger(__name__)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

# Alias `ui.server` to this module so handler modules' ``from ui.server import
# STATE, load_params, ...`` resolves to the single instance (tests load this
# file as the top-level ``server``). Done before the engine imports so nothing
# re-executes this file.
if "ui.server" not in sys.modules:
    sys.modules["ui.server"] = sys.modules[__name__]

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

# App version from pyproject.toml
with open(REPO_ROOT / "pyproject.toml", "rb") as f:
    APP_VERSION = tomllib.load(f)["project"]["version"]


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


# ---------------------------------------------------------------------------
# Handler modules are imported *after* the shared helpers above so their
# ``from ui.server import STATE, load_params, ...`` (etc.) resolve to this
# single module instance without re-executing it.
# ---------------------------------------------------------------------------
from ui.handlers import board as _board  # noqa: E402
from ui.handlers import nets as _nets  # noqa: E402
from ui.handlers import placement as _placement  # noqa: E402
from ui.handlers import routing as _routing  # noqa: E402

_HANDLERS = (_placement, _routing, _nets, _board)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str, headers=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        for mod in _HANDLERS:
            result = mod.handle_get(parsed.path, qs, self)
            if result is not None:
                self._send(*result)
                return
        # Non-API paths fall through to the static UI fallback.
        if not parsed.path.startswith("/api/"):
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
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        for mod in _HANDLERS:
            result = mod.handle_post(parsed.path, qs, body, self)
            if result is not None:
                self._send(*result)
                return
        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # keep console quiet; capturable via logging
        logger.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Workflow UI: http://127.0.0.1:{port}", flush=True)

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
        shutdown(signal.SIGINT, None)
        _shutting_down.wait(timeout=5.0)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
