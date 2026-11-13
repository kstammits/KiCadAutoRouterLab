"""Workflow UI HTTP server: dispatch + shared state.

This module keeps the public names tests and scripts rely on
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


# Pure payload builders live in ui/server_payloads.py; re-exported here so
# ``from ui.server import proposal_to_json, ...`` keeps working.
from ui.server_payloads import (  # noqa: E402
    _footprint_summary,
    _is_within,
    _movable_uuids_for,
    _routing_result_payload,
    _safe_filename,
    proposal_to_json,
)


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
            if not _is_within(UI_DIR.resolve(), full):
                self._send(403, b"forbidden", "text/plain")
                return
            if not full.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = _STATIC_MIME.get(full.suffix, "application/octet-stream")
            self._send(200, full.read_bytes(), ctype,
                       headers={"Cache-Control": "no-store"})
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
