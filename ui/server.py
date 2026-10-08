import base64
import io
import json
import signal
import sys
import threading
import tomllib
from typing import Dict, Tuple, Set, Optional, Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image

UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent.resolve()
sys.path.insert(0, str(UI_DIR.parent / "src"))

from kicad_autorouter.board_model import apply_deltas, board_model, commit_placement
from kicad_autorouter.drc import TMP_DIR, run_drc_on_tree
from kicad_autorouter.io import nudge_footprint_by_uuid, rip_up_nets
from kicad_autorouter.pipeline import stages
from kicad_autorouter.placement import (
    PlacementParams,
    PlacementProposal,
    run_placement,
)
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
from kicad_autorouter.routing.power import identify_power_nets
from kicad_autorouter.sexpr import parse
from kicad_autorouter.svg_render import render_board_svg

# App version from pyproject.toml
with open(REPO_ROOT / "pyproject.toml", "rb") as f:
    APP_VERSION = tomllib.load(f)["project"]["version"]

# User-tuned placement parameters persist here (gitignored).
PARAMS_PATH = REPO_ROOT / "placement.json"


class BoardState:
    """In-memory snapshot of the currently loaded board pair + proposal + undo stack."""

    def __init__(self) -> None:
        # RLock: accept_proposal holds the lock while calling push_undo, which re-acquires it.
        self._lock = threading.RLock()
        self.model = None
        self.sch = None
        self.pcb_tree = None  # Original S-expression tree for writeback
        self.original_pcb_tree = None  # Original tree for reset
        self.name = ""
        self.version = 0
        self.proposal = None
        self.forces: Dict[str, Tuple[float, float]] = {}  # Cached forces for display
        self.pinned_uuids: set[str] = set()  # User-pinned (pseudo-locked)
        self.selected_uuids: set[str] = set()  # Currently selected for move
        self.drc_violations: list[dict] = []  # Cached DRC violations for overlay
        self.drc_for_version: Optional[int] = None  # Board version the cached result was computed against
        self.drc_unconnected: int = 0  # Unconnected item count from last DRC run
        self.drc_running: bool = False  # A kicad-cli DRC subprocess is in flight
        self.drc_error: Optional[str] = None  # Error message from last DRC run, if any
        self.undo_stack: list[tuple] = []  # Stack of (model, pcb_tree) for undo
        self.max_undo_depth = 20
        self.board_version: str = ""  # KiCad generator_version (e.g., "10.0")
        self.version_warning: Optional[str] = None  # Warning for v11+ boards
        # Routing state
        self.protected_nets: set[str] = set()  # Nets protected from rip-up (power/ground + user)
        self.cost_grid: Optional[np.ndarray] = None  # Persistent occupancy grid for incremental routing
        self.routing_grid: Optional[Any] = None  # Persistent RoutingGrid for reuse
        self.routing_model: Optional[Any] = None  # Updated BoardModel after routing

    def load(self, data: bytes, name: str, kind: str) -> int:
        tree = parse(data.decode("utf-8"))
        expected = "kicad_sch" if kind == "sch" else "kicad_pcb"
        if tree.head != expected:
            raise ValueError(f"expected ({expected} ...), got head {tree.head!r}")
        with self._lock:
            if kind == "sch":
                self.sch = tree
                self.board_version = ""
                self.version_warning = None
            else:
                self.model = board_model(tree)
                self.pcb_tree = tree
                self.original_pcb_tree = tree
                self.name = name
                self.sch = None  # a new board invalidates any stale sibling
                self.proposal = None  # a new board invalidates any stale proposal
                self.forces.clear()  # clear cached forces
                self.pinned_uuids.clear()
                self.selected_uuids.clear()
                self.undo_stack.clear()
                # A new board invalidates any cached DRC result
                self.drc_violations.clear()
                self.drc_for_version = None
                self.drc_unconnected = 0
                self.drc_error = None
                # Reset routing state
                self.cost_grid = None
                self.routing_grid = None
                self.routing_model = None
                # Auto-detect protected nets (power/ground)
                self.protected_nets.clear()
                power_nets, ground_nets, _ = identify_power_nets(self.model)
                self.protected_nets.update(power_nets)
                self.protected_nets.update(ground_nets)
                self.protected_nets.update({"GND", "GND_PWR", "VCC", "VDD", "VSS", "GROUND"})
                # Capture board version and warning
                self.board_version = self.model.version if self.model else ""
                self.version_warning = self.model.version_warning if self.model else None
            self.version += 1
            return self.version

    @property
    def snapshot(self):
        with self._lock:
            return (
                self.model,
                self.sch is not None,
                self.name,
                self.version,
                self.board_version,
                self.version_warning,
            )

    @property
    def current_proposal(self):
        with self._lock:
            return self.proposal

    def set_proposal(self, proposal) -> int:
        with self._lock:
            self.proposal = proposal
            self.version += 1
            return self.version

    def get_pinned_uuids(self) -> set[str]:
        with self._lock:
            return self.pinned_uuids.copy()

    def get_selected_uuids(self) -> set[str]:
        with self._lock:
            return self.selected_uuids.copy()

    def get_forces(self) -> Dict[str, Tuple[float, float]]:
        with self._lock:
            return self.forces.copy()

    def set_forces(self, forces: Dict[str, Tuple[float, float]]) -> int:
        with self._lock:
            self.forces = forces.copy()
            self.version += 1
            return self.version

    def get_drc_violations(self) -> list[dict]:
        with self._lock:
            return self.drc_violations.copy()

    def set_drc_violations(self, violations: list[dict]) -> int:
        with self._lock:
            self.drc_violations = violations.copy()
            self.version += 1
            return self.version

    def snapshot_pcb(self):
        """Return (pcb_tree, version) under a single lock acquisition."""
        with self._lock:
            return self.pcb_tree, self.version

    def get_drc_status(self) -> dict:
        """Lightweight DRC cache state for the UI status bar."""
        with self._lock:
            return {
                "running": self.drc_running,
                "for_version": self.drc_for_version,
                "current_version": self.version,
                "stale": self.drc_for_version != self.version,
                "error": self.drc_error,
                "violations": len(self.drc_violations),
                "unconnected": self.drc_unconnected,
            }

    def set_drc_running(self, running: bool) -> None:
        with self._lock:
            self.drc_running = running

    def store_drc_result(
        self,
        violations: list[dict],
        for_version: int,
        unconnected: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """Store DRC results stamped with the board version they describe.

        Does not bump ``self.version`` — DRC output is derived state and must
        not invalidate itself on arrival.
        """
        with self._lock:
            self.drc_violations = list(violations)
            self.drc_for_version = for_version
            self.drc_unconnected = unconnected
            self.drc_error = error

    def set_pinned_uuids(self, uuids: set[str]) -> int:
        with self._lock:
            self.pinned_uuids = uuids.copy()
            self.version += 1
            return self.version

    def set_selected_uuids(self, uuids: set[str]) -> int:
        with self._lock:
            self.selected_uuids = uuids.copy()
            self.version += 1
            return self.version

    def toggle_pinned(self, uuid: str) -> int:
        with self._lock:
            if uuid in self.pinned_uuids:
                self.pinned_uuids.remove(uuid)
            else:
                self.pinned_uuids.add(uuid)
            self.version += 1
            return self.version

    def toggle_selected(self, uuid: str) -> int:
        with self._lock:
            if uuid in self.selected_uuids:
                self.selected_uuids.remove(uuid)
            else:
                self.selected_uuids.add(uuid)
            self.version += 1
            return self.version

    def clear_selection(self) -> int:
        with self._lock:
            self.selected_uuids.clear()
            self.version += 1
            return self.version

    def select_all_unlocked(self, model) -> int:
        """Select all unlocked, non-pinned footprints."""
        with self._lock:
            self.selected_uuids = {
                fp.uuid for fp in model.footprints
                if fp.uuid and not fp.locked and fp.uuid not in self.pinned_uuids
            }
            self.version += 1
            return self.version

    # ==================== Protected Nets & Routing State ====================

    def get_protected_nets(self) -> set[str]:
        """Return copy of protected nets set."""
        with self._lock:
            return self.protected_nets.copy()

    def set_protected_nets(self, nets: set[str]) -> int:
        """Replace the entire protected nets set."""
        with self._lock:
            self.protected_nets = set(nets)
            self.version += 1
            return self.version

    def toggle_protected_net(self, net_name: str) -> int:
        """Toggle a net's protected status."""
        with self._lock:
            if net_name in self.protected_nets:
                self.protected_nets.remove(net_name)
            else:
                self.protected_nets.add(net_name)
            self.version += 1
            return self.version

    def auto_detect_protected_nets(self) -> int:
        """Auto-detect power/ground nets and add them to protected set."""
        if self.model is None:
            return self.version
        power_nets, ground_nets, _ = identify_power_nets(self.model)
        with self._lock:
            self.protected_nets.update(power_nets)
            self.protected_nets.update(ground_nets)
            self.protected_nets.update({"GND", "GND_PWR", "VCC", "VDD", "VSS", "GROUND"})
            self.version += 1
            return self.version

    def get_routing_state(self) -> dict:
        """Get current routing state for UI."""
        with self._lock:
            return {
                "has_cost_grid": self.cost_grid is not None,
                "has_routing_grid": self.routing_grid is not None,
                "protected_nets": sorted(self.protected_nets),
            }

    def reset_routing_state(self) -> int:
        """Clear routing state (cost_grid, routing_grid, routing_model)."""
        with self._lock:
            self.cost_grid = None
            self.routing_grid = None
            self.routing_model = None
            self.version += 1
            return self.version

    def push_undo(self) -> None:
        """Push current state to undo stack (called before mutating)."""
        with self._lock:
            if self.model is not None and self.pcb_tree is not None:
                self.undo_stack.append((self.model, self.pcb_tree))
                if len(self.undo_stack) > self.max_undo_depth:
                    self.undo_stack.pop(0)

    def undo(self) -> int:
        """Revert to previous state from undo stack."""
        with self._lock:
            if not self.undo_stack:
                return self.version
            self.model, self.pcb_tree = self.undo_stack.pop()
            self.proposal = None
            self.selected_uuids.clear()
            # Reset routing state on undo
            self.cost_grid = None
            self.routing_grid = None
            self.routing_model = None
            self.version += 1
            return self.version

    def reset_to_original(self) -> int:
        """Reset to the originally loaded board."""
        with self._lock:
            if self.original_pcb_tree is not None:
                self.model = board_model(self.original_pcb_tree)
                self.pcb_tree = self.original_pcb_tree
                self.proposal = None
                self.selected_uuids.clear()
                self.undo_stack.clear()
                # Reset routing state
                self.cost_grid = None
                self.routing_grid = None
                self.routing_model = None
                # Restore board version and warning from original
                self.board_version = self.model.version if self.model else ""
                self.version_warning = self.model.version_warning if self.model else None
                self.version += 1
            return self.version

    def _get_affected_nets(self, moved_uuids: Set[str]) -> Set[str]:
        """Collect net names from pads of moved footprints."""
        affected = set()
        if self.model is None:
            return affected
        for fp in self.model.footprints:
            if fp.uuid in moved_uuids:
                for pad in fp.pads:
                    if pad.net_name:
                        affected.add(pad.net_name)
        return affected

    def accept_proposal(self) -> int:
        """Apply current proposal to both model and PCB tree, push to undo stack."""
        with self._lock:
            if self.proposal is None or self.model is None or self.pcb_tree is None:
                raise ValueError("no proposal to accept")
            # Push current state to undo before applying
            self.push_undo()
            # Identify affected nets from moved footprints
            moved_uuids = {
                u for u, d in self.proposal.deltas.items()
                if d != (0.0, 0.0, 0.0) and d != (0.0, 0.0)
            }
            # Use protected nets from BoardState (includes auto-detected + user-added)
            protected_nets = self.get_protected_nets()
            # Apply to BoardModel: move footprints + rip up tracks/vias on affected nets
            self.model = commit_placement(self.model, self.proposal.deltas, protected_nets)
            # Apply to S-expression tree: move footprints + rip up tracks/vias
            self.pcb_tree = rip_up_nets(self.pcb_tree, self._get_affected_nets(moved_uuids), protected_nets)
            for uuid, (dx, dy, da) in self.proposal.deltas.items():
                if dx != 0.0 or dy != 0.0 or da != 0.0:
                    self.pcb_tree = nudge_footprint_by_uuid(self.pcb_tree, uuid, dx, dy, da)
            self.proposal = None
            self.version += 1
            return self.version

    def get_pcb_tree(self):
        """Get the current PCB S-expression tree for download."""
        with self._lock:
            return self.pcb_tree


STATE = BoardState()

# Single-flight DRC: at most one kicad-cli subprocess at a time. Concurrent
# callers block on this lock, then serve the fresh cache instead of re-running.
_drc_lock = threading.Lock()
DRC_PCB_PATH = TMP_DIR / "drc_current.kicad_pcb"
DRC_REPORT_PATH = TMP_DIR / "drc_report_current.json"


def _run_drc_now(tree, for_version: int) -> dict:
    """Run kicad-cli DRC on ``tree`` and store the result stamped with ``for_version``."""
    STATE.set_drc_running(True)
    try:
        result = run_drc_on_tree(tree, out_path=DRC_PCB_PATH, report_path=DRC_REPORT_PATH)
    finally:
        STATE.set_drc_running(False)
    filtered = result.filter_violations()
    by_type: Dict[str, int] = {}
    for v in filtered:
        t = v.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    STATE.store_drc_result(
        filtered,
        for_version=for_version,
        unconnected=result.unconnected_count,
        error=result.error,
    )
    current_version = STATE.snapshot[3]
    return {
        "ok": True,
        "cached": False,
        "for_version": for_version,
        "current_version": current_version,
        "stale": for_version != current_version,
        "violations": len(filtered),
        "unconnected": result.unconnected_count,
        "by_type": by_type,
        "error": result.error,
    }


def load_params() -> PlacementParams:
    """Effective params: defaults overlaid with placement.json when present."""
    if PARAMS_PATH.is_file():
        try:
            data = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
            return PlacementParams.from_dict(data)
        except (ValueError, json.JSONDecodeError):
            pass  # corrupt file falls back to defaults; next save overwrites it
    return PlacementParams()


def load_routing_params() -> RoutingParams:
    """Load routing params from placement.json routing section."""
    if PARAMS_PATH.is_file():
        try:
            data = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
            routing_data = data.get("routing", {})
            return RoutingParams.from_dict(routing_data)
        except (ValueError, json.JSONDecodeError):
            pass
    return RoutingParams()


def save_params(params: PlacementParams) -> None:
    # Load existing data to preserve routing section
    existing = {}
    if PARAMS_PATH.is_file():
        try:
            existing = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            pass
    # Update placement params
    existing.update(params.to_dict())
    PARAMS_PATH.write_text(
        json.dumps(existing, indent=2) + "\n", encoding="utf-8"
    )


def save_routing_params(params: RoutingParams) -> None:
    # Load existing data to preserve placement section
    existing = {}
    if PARAMS_PATH.is_file():
        try:
            existing = json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            pass
    # Update routing params
    existing["routing"] = params.to_dict()
    PARAMS_PATH.write_text(
        json.dumps(existing, indent=2) + "\n", encoding="utf-8"
    )


def _cost_grid_to_debug(cost_grid: np.ndarray, routing_grid) -> dict:
    """Convert cost grid to downsampled debug format for SVG overlay.
    
    Cost values:
    - 0 = FREE
    - 50 = HIGH_COST (pad clearance)
    - 80 = EDGE_KEEPOUT (board edge)
    - 100+ = BLOCKED (tracks, vias, courtyards, zones)
    """
    n_layers, H, W = cost_grid.shape
    
    # Downsample to max 200x200 for reasonable JSON size
    max_dim = 200
    scale = max(H // max_dim, W // max_dim, 1)
    
    if scale > 1:
        # Take max in each block to preserve blocked cells
        h_ds = H // scale
        w_ds = W // scale
        ds_grid = np.zeros((n_layers, h_ds, w_ds), dtype=np.int16)
        for l in range(n_layers):
            for r in range(h_ds):
                for c in range(w_ds):
                    r0, r1 = r * scale, min((r + 1) * scale, H)
                    c0, c1 = c * scale, min((c + 1) * scale, W)
                    ds_grid[l, r, c] = np.max(cost_grid[l, r0:r1, c0:c1])
    else:
        ds_grid = cost_grid
        h_ds, w_ds = H, W
    
    # Convert to lists for JSON
    layers = []
    for l in range(n_layers):
        layer_data = ds_grid[l].tolist()
        # Find min/max for color scaling
        vals = [v for row in layer_data for v in row if v > 0]
        layers.append({
            "layer": routing_grid.layers[l] if l < len(routing_grid.layers) else f"layer_{l}",
            "data": layer_data,
            "height": h_ds,
            "width": w_ds,
            "scale": scale,
            "min_cost": min(vals) if vals else 0,
            "max_cost": max(vals) if vals else 0,
        })
    
    return {
        "layers": layers,
        "origin_mm": routing_grid.origin_mm,
        "resolution_mm": routing_grid.resolution_mm * scale,
        "cost_scale": {
            "FREE": 0,
            "HIGH_COST": 50,
            "EDGE_KEEPOUT": 80,
            "BLOCKED": 100,
        }
    }


def _cost_grid_to_png(cost_grid: np.ndarray, routing_grid) -> str:
    """Convert cost grid to base64-encoded PNG for SVG overlay.
    
    Returns base64-encoded PNG data URL.
    """
    n_layers, H, W = cost_grid.shape
    
    # Use F.Cu layer (layer 0)
    layer_data = cost_grid[0]
    
    # Create RGBA array directly
    # Colors: 0=transparent, 50=yellow (HIGH_COST), 80=orange (EDGE_KEEPOUT), 100+=red (BLOCKED)
    rgba = np.zeros((H, W, 4), dtype=np.uint8)
    
    # Mask for non-zero costs
    mask = layer_data > 0
    
    # HIGH_COST (50) -> yellow
    high_cost = (layer_data == 50) & mask
    rgba[high_cost] = [255, 255, 0, 180]
    
    # EDGE_KEEPOUT (80) -> orange
    edge_keepout = (layer_data == 80) & mask
    rgba[edge_keepout] = [255, 165, 0, 200]
    
    # BLOCKED (100+) -> red
    blocked = (layer_data >= 100) & mask
    rgba[blocked] = [255, 0, 0, 220]
    
    # Create image
    img_rgba = Image.fromarray(rgba, mode='RGBA')
    
    # Save to bytes
    buf = io.BytesIO()
    img_rgba.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    
    b64 = base64.b64encode(buf.read()).decode()
    return f"data:image/png;base64,{b64}"


def proposal_to_json(proposal) -> dict:
    def r5(x):
        return round(x, 5) if isinstance(x, float) else x

    return {
        "deltas": {u: [r5(dx), r5(dy), r5(da)] for u, (dx, dy, da) in proposal.deltas.items()},
        "iterations": proposal.iterations,
        "final_max_disp_mm": r5(proposal.final_max_disp_mm),
        "elapsed_s": r5(proposal.elapsed_s),
        "params": proposal.params,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/api/stages":
            self._send(200, json.dumps(stages()).encode(), "application/json")
            return
        if self.path == "/api/version":
            self._send(200, json.dumps({"version": APP_VERSION}).encode(), "application/json")
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/state":
            model, has_sch, name, version, board_version, version_warning = STATE.snapshot
            body = json.dumps(
                {
                    "loaded": model is not None,
                    "name": name,
                    "version": version,
                    "has_sch": has_sch,
                    "board_version": board_version,
                    "warning": version_warning,
                }
            )
            self._send(200, body.encode(), "application/json")
            return
        if parsed.path == "/api/drc/status":
            self._send(200, json.dumps(STATE.get_drc_status()).encode(), "application/json")
            return
        if parsed.path == "/api/board.svg":
            model, _, name, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            overlay_on = qs.get("overlay", ["1"])[0] != "0"
            forces_on = qs.get("forces", ["0"])[0] != "0"
            drc_on = qs.get("drc", ["0"])[0] != "0"
            fcu_on = qs.get("fcu", ["1"])[0] != "0"
            bcu_on = qs.get("bcu", ["1"])[0] != "0"
            proposal = STATE.current_proposal if overlay_on else None
            pinned = STATE.get_pinned_uuids()
            selected = STATE.get_selected_uuids()
            
            # Create a minimal proposal for forces display if needed
            if forces_on and proposal is None:
                from kicad_autorouter.placement import PlacementProposal
                proposal = PlacementProposal(
                    deltas={},
                    iterations=0,
                    final_max_disp_mm=0.0,
                    elapsed_s=0.0,
                    params={},
                    forces=STATE.get_forces(),
                )
            elif forces_on and proposal is not None:
                # Merge stored forces into existing proposal
                stored_forces = STATE.get_forces()
                if stored_forces:
                    merged_forces = {**proposal.forces, **stored_forces}
                    proposal = PlacementProposal(
                        deltas=proposal.deltas,
                        iterations=proposal.iterations,
                        final_max_disp_mm=proposal.final_max_disp_mm,
                        elapsed_s=proposal.elapsed_s,
                        params=proposal.params,
                        forces=merged_forces,
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

            svg = render_board_svg(model, title=name, proposal=proposal,
                                   pinned_uuids=pinned, selected_uuids=selected,
                                   show_forces=forces_on,
                                   drc_violations=drc_violations,
                                   drc_stale=drc_stale).encode()
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
            filename = STATE.name.replace(".kicad_pcb", "-modified.kicad_pcb")
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
            footprints_data = []
            # Real footprints
            for fp in model.footprints:
                if not fp.uuid:
                    continue
                # Collect unique net names for this footprint
                nets = sorted({pad.net_name for pad in fp.pads if pad.net_name})
                # Courtyard bbox
                minx = miny = float('inf')
                maxx = maxy = float('-inf')
                for a, b in fp.courtyard:
                    minx = min(minx, a.x_mm, b.x_mm)
                    miny = min(miny, a.y_mm, b.y_mm)
                    maxx = max(maxx, a.x_mm, b.x_mm)
                    maxy = max(maxy, a.y_mm, b.y_mm)
                if minx == float('inf'):
                    minx = miny = maxx = maxy = 0.0
                footprints_data.append({
                    "uuid": fp.uuid,
                    "ref": fp.ref,
                    "footprint_id": fp.footprint_id,
                    "layer": fp.layer,
                    "x_mm": fp.x_mm,
                    "y_mm": fp.y_mm,
                    "angle_deg": fp.angle_deg,
                    "locked": fp.locked,
                    "pinned": fp.uuid in pinned,
                    "selected": fp.uuid in selected,
                    "nets": nets,
                    "bbox": [minx, miny, maxx, maxy],
                    "ghost": False,
                })
            # Ghost footprints
            for fp in model.ghost_footprints:
                if not fp.uuid:
                    continue
                nets = sorted({pad.net_name for pad in fp.pads if pad.net_name})
                minx = miny = float('inf')
                maxx = maxy = float('-inf')
                for a, b in fp.courtyard:
                    minx = min(minx, a.x_mm, b.x_mm)
                    miny = min(miny, a.y_mm, b.y_mm)
                    maxx = max(maxx, a.x_mm, b.x_mm)
                    maxy = max(maxy, a.y_mm, b.y_mm)
                if minx == float('inf'):
                    minx = miny = maxx = maxy = 0.0
                footprints_data.append({
                    "uuid": fp.uuid,
                    "ref": fp.ref,
                    "footprint_id": fp.footprint_id,
                    "layer": fp.layer,
                    "x_mm": fp.x_mm,
                    "y_mm": fp.y_mm,
                    "angle_deg": fp.angle_deg,
                    "locked": False,  # Ghosts are never locked in the traditional sense
                    "pinned": False,
                    "selected": False,  # Ghosts can't be selected/pinned
                    "nets": nets,
                    "bbox": [minx, miny, maxx, maxy],
                    "ghost": True,
                })
            self._send(200, json.dumps({"ok": True, "footprints": footprints_data}).encode(), "application/json")
            return
        
        if parsed.path == "/api/nets":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            protected = STATE.get_protected_nets()
            # Count existing tracks/vias per net
            track_counts = {}
            via_counts = {}
            for t in model.tracks:
                if t.net_name:
                    track_counts[t.net_name] = track_counts.get(t.net_name, 0) + 1
            for v in model.vias:
                if v.net_name:
                    via_counts[v.net_name] = via_counts.get(v.net_name, 0) + 1
            
            nets_data = []
            for net_name, conns in model.nets.items():
                footprint_refs = set()
                for conn in conns:
                    footprint_refs.add(conn.ref)
                nets_data.append({
                    "name": net_name,
                    "pad_count": len(conns),
                    "footprint_count": len(footprint_refs),
                    "track_count": track_counts.get(net_name, 0),
                    "via_count": via_counts.get(net_name, 0),
                    "protected": net_name in protected,
                })
            # Sort: protected first, then by pad count descending
            nets_data.sort(key=lambda n: (not n["protected"], -n["pad_count"]))
            self._send(200, json.dumps({"ok": True, "nets": nets_data}).encode(), "application/json")
            return
        
        # GET /api/routing/debug/cost-grid - return cost grid for visualization
        if parsed.path == "/api/routing/debug/cost-grid":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            if STATE.cost_grid is None or STATE.routing_grid is None:
                self._send(404, b"no cost grid available (run routing first)", "text/plain")
                return
            payload = _cost_grid_to_debug(STATE.cost_grid, STATE.routing_grid)
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        
        # GET /api/routing/debug/cost-grid.png - return cost grid as PNG for SVG overlay
        if parsed.path == "/api/routing/debug/cost-grid.png":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            if STATE.cost_grid is None or STATE.routing_grid is None:
                self._send(404, b"no cost grid available (run routing first)", "text/plain")
                return
            try:
                png_data_url = _cost_grid_to_png(STATE.cost_grid, STATE.routing_grid)
                payload = json.dumps({"png": png_data_url}).encode()
                self._send(200, payload, "application/json")
            except Exception as exc:
                self._send(500, f"PNG generation failed: {exc}".encode(), "text/plain")
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
        ctype = "text/html" if full.suffix == ".html" else "application/octet-stream"
        self._send(200, full.read_bytes(), ctype)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/load":
            kind = "sch" if (qs.get("kind") or ["pcb"])[0] == "sch" else "pcb"
            name = (qs.get("name") or ["board.kicad_pcb"])[0]
            path_q = (qs.get("path") or [None])[0]
            if path_q:
                full = (REPO_ROOT / path_q).resolve()
                if not str(full).startswith(str(REPO_ROOT)) or not full.is_file():
                    self._send(400, b"bad path", "text/plain")
                    return
                data = full.read_bytes()
            else:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._send(400, b"empty body", "text/plain")
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
                params = PlacementParams.from_dict(json.loads(data.decode("utf-8")))
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
            req_stub = req.get("stub", params.stub)
            if iterations is not None:
                if not isinstance(iterations, int) or (iterations < 1 and not req_stub):
                    self._send(400, b"iterations must be a positive integer (or 0 for stub mode)", "text/plain")
                    return
                # Create params with overridden max_iterations, preserve other settings
                params = PlacementParams(
                    repulsion_kr=params.repulsion_kr,
                    attraction_ka=params.attraction_ka,
                    ideal_length_mm=params.ideal_length_mm,
                    max_iterations=iterations,
                    convergence_eps_mm=params.convergence_eps_mm,
                    rigid_stiffness=params.rigid_stiffness,
                    courtyard_repulsion_kc=params.courtyard_repulsion_kc,
                    demo_jitter_mm=params.demo_jitter_mm,
                    stub=req_stub,
                )

# If movable_uuids not provided, compute as all unlocked except pinned
            if movable_uuids is None:
                movable_uuids = {
                    fp.uuid for fp in model.footprints
                    if fp.uuid and not fp.locked and fp.uuid not in STATE.get_pinned_uuids()
                }
            else:
                # Filter out any pinned footprints from explicit movable_uuids
                pinned = STATE.get_pinned_uuids()
                movable_uuids = {u for u in movable_uuids if u not in pinned}
            
            
            try:
                proposal = run_placement(model, params, movable_uuids)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                self._send(500, f"placement failed: {exc}".encode(), "text/plain")
                return
            STATE.set_forces(proposal.forces)
            version = STATE.set_proposal(proposal)
            payload = {
                "ok": True,
                "moved": len(proposal.deltas),
                "iterations": proposal.iterations,
                "final_max_disp_mm": proposal.final_max_disp_mm,
                "elapsed_s": proposal.elapsed_s,
                "version": version,
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

            # Use parameters from request if provided, else load from file
            base_params = load_params()
            params = PlacementParams(
                repulsion_kr=req.get("repulsion_kr", base_params.repulsion_kr),
                attraction_ka=req.get("attraction_ka", base_params.attraction_ka),
                ideal_length_mm=req.get("ideal_length_mm", base_params.ideal_length_mm),
                max_iterations=0,
                convergence_eps_mm=req.get("convergence_eps_mm", base_params.convergence_eps_mm),
                rigid_stiffness=req.get("rigid_stiffness", base_params.rigid_stiffness),
                courtyard_repulsion_kc=req.get("courtyard_repulsion_kc", base_params.courtyard_repulsion_kc),
                demo_jitter_mm=req.get("demo_jitter_mm", base_params.demo_jitter_mm),
                stub=False,  # Force physics mode for force computation
            )

            # If movable_uuids not provided, compute as all unlocked except pinned
            if movable_uuids is None:
                movable_uuids = {
                    fp.uuid for fp in model.footprints
                    if fp.uuid and not fp.locked and fp.uuid not in STATE.get_pinned_uuids()
                }
            else:
                # Filter out any pinned footprints from explicit movable_uuids
                pinned = STATE.get_pinned_uuids()
                movable_uuids = {u for u in movable_uuids if u not in pinned}

            try:
                proposal = run_placement(model, params, movable_uuids)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                self._send(500, f"placement failed: {exc}".encode(), "text/plain")
                return

            # Store forces in BoardState for SVG rendering
            STATE.set_forces(proposal.forces)

            payload = {
                "ok": True,
                "forces": {u: [fx, fy] for u, (fx, fy) in proposal.forces.items()},
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if parsed.path == "/api/placement/accept":
            try:
                version = STATE.accept_proposal()
            except ValueError as exc:
                self._send(400, f"{exc}".encode(), "text/plain")
                return
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
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
        if parsed.path == "/api/board/reset":
            version = STATE.reset_to_original()
            self._send(
                200,
                json.dumps({"ok": True, "version": version}).encode(),
                "application/json",
            )
            return
        if parsed.path == "/api/placement/apply":
            # Writeback (nudge by UUID + save_pair + DRC) lands in PLAN item 3.
            self._send(
                501, b"writeback not implemented yet (PLAN item 3)", "text/plain"
            )
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
            version = STATE.toggle_pinned(uuid)
            pinned = STATE.get_pinned_uuids()
            self._send(200, json.dumps({"ok": True, "pinned": list(pinned), "version": version}).encode(), "application/json")
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
            version = STATE.toggle_selected(uuid)
            selected = STATE.get_selected_uuids()
            self._send(200, json.dumps({"ok": True, "selected": list(selected), "version": version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/select_all":
            model, _, _, _, _, _ = STATE.snapshot
            if model is None:
                self._send(404, b"no board loaded", "text/plain")
                return
            version = STATE.select_all_unlocked(model)
            selected = STATE.get_selected_uuids()
            self._send(200, json.dumps({"ok": True, "selected": list(selected), "version": version}).encode(), "application/json")
            return
        if parsed.path == "/api/placement/clear_selection":
            version = STATE.clear_selection()
            self._send(200, json.dumps({"ok": True, "version": version}).encode(), "application/json")
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
            # Writeback (nudge by UUID + save_pair + DRC) lands in PLAN item 3.
            self._send(
                501, b"writeback not implemented yet (PLAN item 3)", "text/plain"
            )
            return
        
        # ==================== Nets API ====================
        
        if parsed.path == "/api/nets/protected":
            if parsed.query_string and "GET" in parsed.query_string:
                pass  # handled by query method
            # Handle via query string method
            pass
        
        if parsed.path == "/api/nets/protected":
            protected = STATE.get_protected_nets()
            self._send(200, json.dumps({"ok": True, "protected": sorted(protected)}).encode(), "application/json")
            return
        
        # POST /api/nets/protected - toggle protected net
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
            if protected_flag:
                version = STATE.toggle_protected_net(net_name)
            else:
                # Explicit set
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
        
        # ==================== Routing API ====================
        
        # GET /api/routing/params
        if parsed.path == "/api/routing/params":
            params = load_routing_params()
            self._send(200, json.dumps({"ok": True, "params": params.to_dict()}).encode(), "application/json")
            return
        
        # POST /api/routing/params - save routing params
        if parsed.path == "/api/routing/params" and self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            body_data = self.rfile.read(length) if length > 0 else b"{}"
            try:
                req = json.loads(body_data.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            params = RoutingParams.from_dict(req)
            save_routing_params(params)
            self._send(200, json.dumps({"ok": True, "params": params.to_dict()}).encode(), "application/json")
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
            if not net_names:
                self._send(400, b"net_names required (non-empty list)", "text/plain")
                return
            # Load routing params from request or file
            route_params_dict = req.get("params", {})
            route_params = RoutingParams.from_dict(route_params_dict)
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
                )
            except Exception as exc:
                import traceback
                traceback.print_exc()
                self._send(500, f"routing failed: {exc}".encode(), "text/plain")
                return
            
            # Update BoardState with new routing state
            with STATE._lock:
                STATE.cost_grid = cost_grid
                STATE.routing_grid = routing_grid
                STATE.routing_model = model
                STATE.pcb_tree = result.pcb_tree
                STATE.version += 1
                version = STATE.version
            
            payload = {
                "ok": True,
                "tracks_added": result.tracks_added,
                "vias_added": result.vias_added,
                "nets_routed": result.nets_routed,
                "nets_failed": result.nets_failed,
                "drc_clean": result.drc_clean,
                "drc_violations": result.drc_violations,
                "version": version,
            }
            # Include debug info if requested
            if route_params.debug:
                payload["debug"] = _cost_grid_to_debug(cost_grid, routing_grid)
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        
        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # keep console quiet
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Workflow UI: http://127.0.0.1:{port}")

    def shutdown(signum, frame):
        print("\nShutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
