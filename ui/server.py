import json
import signal
import sys
import threading
import time
import tomllib
import uuid as uuidlib
from dataclasses import replace
from typing import Dict, Tuple, Set, Optional, Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

UI_DIR = Path(__file__).resolve().parent
REPO_ROOT = UI_DIR.parent.resolve()
sys.path.insert(0, str(UI_DIR.parent / "src"))

from kicad_autorouter.board_model import (
    apply_deltas,
    board_model,
    commit_placement,
    is_ground_net,
    is_power_net_effective,
    resolve_track_width_mm,
    seed_power_nets_from_netclasses,
)
from kicad_autorouter.drc import TMP_DIR, run_drc_on_tree
from kicad_autorouter.ghost import make_ghost_attractor, make_ghost_wall
from kicad_autorouter.io import (
    add_ghost_to_tree,
    nudge_footprint_by_uuid,
    remove_ghost_from_tree,
    resize_ghost_in_tree,
    rip_up_nets,
)
from kicad_autorouter.pipeline import stages
from kicad_autorouter.placement import (
    PlacementParams,
    PlacementProposal,
    run_placement,
)
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets
from kicad_autorouter.routing.grid import create_grid_from_model
from kicad_autorouter.routing.pour import gnd_islands
from kicad_autorouter.routing.power import identify_power_nets
from kicad_autorouter.sexpr import parse
from kicad_autorouter.svg_render import decap_fp_uuids, net_colors, render_board_svg

# App version from pyproject.toml
with open(REPO_ROOT / "pyproject.toml", "rb") as f:
    APP_VERSION = tomllib.load(f)["project"]["version"]

# User-tuned placement parameters persist here (gitignored).
PARAMS_PATH = REPO_ROOT / "placement.json"

# Curated docs servable via /api/docs/*. Large KiCad manuals and wikipedia
# sources are deliberately excluded (pcbnew.md alone is ~700KB and would
# freeze the side panel on inject).
DOCS_ALLOWLIST: Dict[str, Path] = {
    "README.md": REPO_ROOT / "README.md",
    "PLAN.md": REPO_ROOT / "PLAN.md",
    "TODO.md": REPO_ROOT / "TODO.md",
    "docs/README.md": REPO_ROOT / "docs" / "README.md",
    "docs/autorouting_glossary.md": REPO_ROOT / "docs" / "autorouting_glossary.md",
}
DOCS_MAX_BYTES = 200 * 1024


def _docs_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
    return fallback


def _docs_entry(name: str, path: Path) -> dict:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    title = name
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(2048)
        title = _docs_title(head, name)
    except OSError:
        pass
    return {"name": name, "title": title, "size_bytes": size}


# Nudge/ghost-move coalescing window (seconds): repeated manual moves of
# the same selection inside this window share one undo entry, so holding
# an arrow key doesn't flush the undo history with 1mm micro-steps. Undo
# then reverts the whole burst as one gesture.
NUDGE_COALESCE_S = 2.0


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
        self.force_components: Dict[str, Dict[str, Tuple[float, float]]] = {}  # Per-cause breakdown
        self.pinned_uuids: set[str] = set()  # User-pinned (pseudo-locked)
        self.selected_uuids: set[str] = set()  # Currently selected for move
        self.drc_violations: list[dict] = []  # Cached DRC violations for overlay
        self.drc_for_version: Optional[int] = None  # Board version the cached result was computed against
        self.drc_unconnected: int = 0  # Unconnected item count from last DRC run
        self.drc_running: bool = False  # A kicad-cli DRC subprocess is in flight
        self.drc_error: Optional[str] = None  # Error message from last DRC run, if any
        self.undo_stack: list[tuple] = []  # Stack of (model, pcb_tree) for undo
        self.max_undo_depth = 20
        # Nudge-burst coalescing: stamp + key set of the last manual move.
        # Any explicit checkpoint (push_undo from another mutator, undo,
        # reset, load) clears these so the next move starts a new entry.
        self._last_nudge_at: float = 0.0
        self._last_nudge_keys: frozenset = frozenset()
        self.board_version: str = ""  # KiCad generator_version (e.g., "10.0")
        self.version_warning: Optional[str] = None  # Warning for v11+ boards
        # Routing state
        self.protected_nets: set[str] = set()  # Nets protected from rip-up (power/ground + user)
        self.power_override_nets: set[str] = set()  # User-marked power membership (nets table ⚡);
        # seeded from power-named KiCad netclasses on load, in-memory only
        # (per-board net names must not leak into the global params file).
        self.cost_grid: Optional[np.ndarray] = None  # Persistent occupancy grid for incremental routing
        self.routing_grid: Optional[Any] = None  # Persistent RoutingGrid for reuse
        self.routing_model: Optional[Any] = None  # Updated BoardModel after routing
        self.last_accept_report: dict = {}  # Report from most recent accept (for UI diag)

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
                self.force_components.clear()
                self.pinned_uuids.clear()
                self.selected_uuids.clear()
                self.undo_stack.clear()
                self._last_nudge_at = 0.0
                self._last_nudge_keys = frozenset()
                # A new board invalidates any cached DRC result
                self.drc_violations.clear()
                self.drc_for_version = None
                self.drc_unconnected = 0
                self.drc_error = None
                # Reset routing state
                self.cost_grid = None
                self.routing_grid = None
                self.routing_model = None
                # Auto-detect protected nets (power/ground, incl. user-marked power)
                self.protected_nets.clear()
                self.power_override_nets.clear()
                self.power_override_nets.update(seed_power_nets_from_netclasses(self.model))
                power_nets, ground_nets, _ = identify_power_nets(
                    self.model, self.power_override_nets
                )
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

    def get_force_components(self) -> Dict[str, Dict[str, Tuple[float, float]]]:
        with self._lock:
            return {u: dict(c) for u, c in self.force_components.items()}

    def set_force_components(
        self, components: Dict[str, Dict[str, Tuple[float, float]]]
    ) -> int:
        with self._lock:
            self.force_components = {u: dict(c) for u, c in components.items()}
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

    def get_power_override_nets(self) -> set[str]:
        """Return copy of the user-marked power-membership set."""
        with self._lock:
            return set(self.power_override_nets)

    def set_power_override_net(self, net_name: str, is_power: bool) -> int:
        """Explicitly mark/unmark a net as power (no toggle quirk)."""
        with self._lock:
            if is_power:
                self.power_override_nets.add(net_name)
            else:
                self.power_override_nets.discard(net_name)
            self.version += 1
            return self.version

    def auto_detect_protected_nets(self) -> int:
        """Auto-detect power/ground nets and add them to protected set."""
        if self.model is None:
            return self.version
        power_nets, ground_nets, _ = identify_power_nets(
            self.model, self.get_power_override_nets()
        )
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
                "power_override_nets": sorted(self.power_override_nets),
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
        """Push current state to undo stack (called before mutating).

        An explicit checkpoint always breaks any open nudge burst, so the
        next manual move starts a fresh undo entry.
        """
        with self._lock:
            if self.model is not None and self.pcb_tree is not None:
                self.undo_stack.append((self.model, self.pcb_tree))
                if len(self.undo_stack) > self.max_undo_depth:
                    self.undo_stack.pop(0)
            self._last_nudge_at = 0.0
            self._last_nudge_keys = frozenset()

    def _maybe_push_undo(self, keys: frozenset) -> None:
        """Checkpoint for manual moves, coalescing rapid bursts.

        Repeated moves of the same key set (selection uuids, or a single
        ghost uuid) inside ``NUDGE_COALESCE_S`` share the pre-burst entry
        already on the stack — one undo reverts the whole gesture. Any
        change of key set or window expiry pushes a new entry. Call with
        the lock held (re-acquires via ``push_undo``; RLock-safe).
        """
        now = time.monotonic()
        if keys != self._last_nudge_keys or now - self._last_nudge_at > NUDGE_COALESCE_S:
            self.push_undo()
        self._last_nudge_at = now
        self._last_nudge_keys = keys

    def undo(self) -> int:
        """Revert to previous state from undo stack."""
        with self._lock:
            if not self.undo_stack:
                return self.version
            self.model, self.pcb_tree = self.undo_stack.pop()
            # An undo breaks any open burst: the next move checkpoints fresh.
            self._last_nudge_at = 0.0
            self._last_nudge_keys = frozenset()
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
                self._last_nudge_at = 0.0
                self._last_nudge_keys = frozenset()
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
        """Apply current proposal to both model and PCB tree, push to undo stack.

        Also records ``last_accept_report`` with per-net rip-up counts so the
        UI can explain e.g. why only one of R1's two nets was ripped (the
        other — GND — is protected and intentionally kept). Routing stays
        manual; this method only rips, never re-routes.
        """
        with self._lock:
            if self.proposal is None or self.model is None or self.pcb_tree is None:
                raise ValueError("no proposal to accept")
            # Identify moved footprints first: empty proposals (e.g. boards
            # with no nets, or an already-converged step) are a no-op and
            # must not pollute the undo stack.
            moved_uuids = {
                u for u, d in self.proposal.deltas.items()
                if d != (0.0, 0.0, 0.0) and d != (0.0, 0.0)
            }
            if not moved_uuids:
                raise ValueError("proposal has no movement to accept")
            # Push current state to undo before applying
            self.push_undo()
            # Use protected nets from BoardState (includes auto-detected + user-added)
            protected_nets = self.get_protected_nets()
            affected_nets = self._get_affected_nets(moved_uuids)
            # Snapshot proposal diagnostics before clearing
            proposal_iterations = getattr(self.proposal, "iterations", 0)
            proposal_disp = getattr(self.proposal, "final_max_disp_mm", 0.0)
            moved_deltas = {
                u: self.proposal.deltas[u] for u in moved_uuids
                if u in self.proposal.deltas
            }
            # Copper counts before (model = authoritative, tree should match)
            from collections import Counter as _Counter
            tracks_before = _Counter(t.net_name for t in self.model.tracks if t.net_name)
            vias_before = _Counter(v.net_name for v in self.model.vias if v.net_name)
            # Apply to BoardModel: move footprints + rip up tracks/vias on affected nets
            self.model = commit_placement(self.model, self.proposal.deltas, protected_nets)
            # Apply to S-expression tree: move footprints + rip up tracks/vias
            self.pcb_tree = rip_up_nets(self.pcb_tree, affected_nets, protected_nets)
            for uuid, (dx, dy, da) in self.proposal.deltas.items():
                if dx != 0.0 or dy != 0.0 or da != 0.0:
                    self.pcb_tree = nudge_footprint_by_uuid(self.pcb_tree, uuid, dx, dy, da)
            # Copper counts after -> per-net ripped report
            tracks_after = _Counter(t.net_name for t in self.model.tracks if t.net_name)
            vias_after = _Counter(v.net_name for v in self.model.vias if v.net_name)
            ripped: dict = {}
            for net in sorted(affected_nets - protected_nets):
                dt = tracks_before.get(net, 0) - tracks_after.get(net, 0)
                dv = vias_before.get(net, 0) - vias_after.get(net, 0)
                if dt or dv:
                    ripped[net] = {"tracks": dt, "vias": dv}
            self.last_accept_report = {
                "moved": len(moved_uuids),
                "iterations": proposal_iterations,
                "final_max_disp_mm": proposal_disp,
                "affected_nets": sorted(affected_nets),
                "ripped": ripped,
                "skipped_protected": sorted(affected_nets & protected_nets),
                "moved_deltas": {u: list(d) for u, d in moved_deltas.items()},
            }
            self.proposal = None
            self.version += 1
            return self.version

    def nudge_footprints(
        self, uuids: Set[str], dx_mm: float, dy_mm: float, da_deg: float = 0.0
    ) -> tuple[int, list[str]]:
        """Manually shift footprints by (dx, dy, da); shared by the UI nudge pad.

        Locked/pinned footprints and unknown UUIDs are skipped (ghosts live
        outside ``model.footprints`` so they never match). Mirrors
        :meth:`accept_proposal` dual-write: the BoardModel moves via
        ``commit_placement`` (rips affected-net copper, keeps protected) and
        the S-expression tree moves via ``rip_up_nets`` +
        ``nudge_footprint_by_uuid``. Any pending proposal is discarded —
        its deltas are posed against the pre-nudge board.
        """
        import math

        for name, val in (("dx_mm", dx_mm), ("dy_mm", dy_mm), ("da_deg", da_deg)):
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(f"{name} must be finite, got {val!r}")
        if dx_mm == 0.0 and dy_mm == 0.0 and da_deg == 0.0:
            raise ValueError("nudge is a no-op (all deltas zero)")
        with self._lock:
            if self.model is None or self.pcb_tree is None:
                raise ValueError("no board loaded")
            pinned = set(self.pinned_uuids)
            movable = [
                fp.uuid for fp in self.model.footprints
                if fp.uuid and fp.uuid in uuids and not fp.locked
                and fp.uuid not in pinned
            ]
            if not movable:
                raise ValueError("nothing movable in nudge set (locked/pinned/unknown)")
            # Rapid arrow-key bursts share one undo entry (see _maybe_push_undo).
            self._maybe_push_undo(frozenset(movable))
            protected_nets = set(self.protected_nets)
            affected_nets = self._get_affected_nets(set(movable))
            deltas = {u: (dx_mm, dy_mm, da_deg) for u in movable}
            self.model = commit_placement(self.model, deltas, protected_nets)
            self.pcb_tree = rip_up_nets(self.pcb_tree, affected_nets, protected_nets)
            for u in movable:
                self.pcb_tree = nudge_footprint_by_uuid(
                    self.pcb_tree, u, dx_mm, dy_mm, da_deg
                )
            self.proposal = None
            self.version += 1
            return self.version, movable

    def get_pcb_tree(self):
        """Get the current PCB S-expression tree for download."""
        with self._lock:
            return self.pcb_tree

    def ghost_create(
        self,
        kind: str,
        ref: str,
        x_mm: float,
        y_mm: float,
        w_mm: float | None = None,
        h_mm: float | None = None,
        target_ref: str | None = None,
        ka: float | None = None,
        size_mm: float = 4.0,
        courtyard_layer: str = "F.CrtYd",
    ) -> tuple[int, str]:
        """Create a wall/attractor ghost, persist it in the PCB tree + model."""
        import math

        if kind not in ("wall", "attractor"):
            raise ValueError(f"kind must be 'wall' or 'attractor', got {kind!r}")
        if courtyard_layer not in ("F.CrtYd", "F.Fab"):
            raise ValueError(
                f"courtyard_layer must be F.CrtYd or F.Fab, got {courtyard_layer!r}"
            )
        with self._lock:
            if self.model is None or self.pcb_tree is None:
                raise ValueError("no board loaded")
            taken_refs = {fp.ref for fp in self.model.footprints if fp.ref}
            taken_refs |= {fp.ref for fp in self.model.ghost_footprints if fp.ref}
            if ref in taken_refs:
                raise ValueError(f"ref {ref!r} already exists on the board")
            if kind == "wall":
                if w_mm is None or h_mm is None:
                    raise ValueError("wall ghosts require w_mm and h_mm")
                fp = make_ghost_wall(ref, x_mm, y_mm, w_mm, h_mm)
                library_id = "Ghost:Wall"
            else:
                if not target_ref:
                    raise ValueError("attractor ghosts require target_ref")
                if ka is None:
                    raise ValueError("attractor ghosts require ka")
                if target_ref not in (self.model.by_ref or {}):
                    valid = sorted(self.model.by_ref or {})[:20]
                    raise ValueError(
                        f"target {target_ref!r} not found on board"
                        + (f" (e.g. {', '.join(valid)})" if valid else "")
                    )
                try:
                    size_f = float(size_mm)
                except (TypeError, ValueError):
                    raise ValueError(f"size_mm must be numeric, got {size_mm!r}")
                if not math.isfinite(size_f) or size_f <= 0:
                    raise ValueError(f"size_mm must be > 0, got {size_mm!r}")
                fp = make_ghost_attractor(ref, x_mm, y_mm, target_ref, ka, size_mm=size_f)
                library_id = "Ghost:Attractor"
            self.push_undo()
            self.pcb_tree = add_ghost_to_tree(
                self.pcb_tree, fp, library_id, courtyard_layer
            )
            self.model = board_model(self.pcb_tree)
            self.proposal = None
            self.version += 1
            return self.version, fp.uuid

    def ghost_move(
        self, uuid: str, dx_mm: float, dy_mm: float, da_deg: float = 0.0
    ) -> int:
        """Shift a ghost anchor by (dx, dy) and/or rotate it by da_deg.

        Ghosts carry no copper to rip. Rotation pivots about the ghost
        anchor (graphics are footprint-local, so the courtyard spins in
        place — a 90° turn swaps a wall's W/H extents).
        """
        import math

        for name, val in (("dx_mm", dx_mm), ("dy_mm", dy_mm), ("da_deg", da_deg)):
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(f"{name} must be finite, got {val!r}")
        if dx_mm == 0.0 and dy_mm == 0.0 and da_deg == 0.0:
            raise ValueError("ghost move is a no-op (all deltas zero)")
        with self._lock:
            if self.model is None or self.pcb_tree is None:
                raise ValueError("no board loaded")
            if not any(fp.uuid == uuid for fp in self.model.ghost_footprints):
                raise ValueError(f"no ghost with uuid {uuid!r}")
            # Stepper bursts share one undo entry (see _maybe_push_undo).
            self._maybe_push_undo(frozenset({uuid}))
            self.pcb_tree = nudge_footprint_by_uuid(
                self.pcb_tree, uuid, dx_mm, dy_mm, da_deg
            )
            self.model = board_model(self.pcb_tree)
            self.proposal = None
            self.version += 1
            return self.version

    def ghost_delete(self, uuid: str) -> int:
        """Remove a ghost footprint from the PCB tree + model."""
        with self._lock:
            if self.model is None or self.pcb_tree is None:
                raise ValueError("no board loaded")
            if not any(fp.uuid == uuid for fp in self.model.ghost_footprints):
                raise ValueError(f"no ghost with uuid {uuid!r}")
            self.push_undo()
            self.pcb_tree = remove_ghost_from_tree(self.pcb_tree, uuid)
            self.model = board_model(self.pcb_tree)
            self.proposal = None
            self.version += 1
            return self.version

    def ghost_resize(self, uuid: str, w_mm: float, h_mm: float) -> int:
        """Resize a ghost's courtyard outline to ``w_mm`` x ``h_mm``.

        Position, angle, pads, and attraction springs are preserved; only
        the ``fp_line`` outline is rewritten (centered on the ghost anchor).
        """
        with self._lock:
            if self.model is None or self.pcb_tree is None:
                raise ValueError("no board loaded")
            if not any(fp.uuid == uuid for fp in self.model.ghost_footprints):
                raise ValueError(f"no ghost with uuid {uuid!r}")
            try:
                new_tree = resize_ghost_in_tree(
                    self.pcb_tree, uuid, float(w_mm), float(h_mm))
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"{exc}")
            self.push_undo()
            self.pcb_tree = new_tree
            self.model = board_model(self.pcb_tree)
            self.proposal = None
            self.version += 1
            return self.version


STATE = BoardState()

# Single-flight DRC: at most one kicad-cli subprocess at a time. Concurrent
# callers block on this lock, then serve the fresh cache instead of re-running.
_drc_lock = threading.Lock()
DRC_PCB_PATH = TMP_DIR / "drc_current.kicad_pcb"
DRC_REPORT_PATH = TMP_DIR / "drc_report_current.json"

# Async routing jobs: POST /api/routing/jobs starts a background thread per
# job (the UI routes one net per job); GET polls progress; POST .../cancel
# sets a cooperative flag honored between nets/attempts/pairs and inside A*
# (every 4096 expansions). Only one routing job runs at a time — a second
# start gets 409 busy — because jobs share STATE.cost_grid/routing_grid.
# The synchronous POST /api/routing/run is unchanged for back-compat/tests.
_JOBS: Dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_ROUTING_JOB_LOCK = threading.Lock()
_MAX_JOBS = 20


def _prune_jobs_locked() -> None:
    """Drop oldest finished jobs past the cap (call with _JOBS_LOCK held)."""
    if len(_JOBS) <= _MAX_JOBS:
        return
    finished = sorted(
        ((jid, j) for jid, j in _JOBS.items() if j.get("state") in ("done", "failed")),
        key=lambda kv: kv[1].get("finished", 0),
    )
    for jid, _ in finished[: len(_JOBS) - _MAX_JOBS]:
        del _JOBS[jid]


def _run_routing_job(job_id: str) -> None:
    """Background worker: route the job's nets, then apply to STATE."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            _ROUTING_JOB_LOCK.release()
            return
    t0 = time.perf_counter()
    model, _, _, _, _, _ = STATE.snapshot
    tree, _ = STATE.snapshot_pcb()
    protected_nets = STATE.get_protected_nets()
    extra_power = STATE.get_power_override_nets()
    with _JOBS_LOCK:
        job["state"] = "running"
        job["started"] = t0
        params_dict = dict(job.get("params_dict") or {})
        net_names = list(job.get("net_names") or [])

    def _should_cancel() -> bool:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            return bool(j and j.get("cancel_requested"))

    def _on_progress(net_name: str, done: int, total: int, expansions: int) -> None:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is None:
                return
            j["progress"] = {
                "stage": "astar",
                "net": net_name,
                "pairs_done": done,
                "pairs_total": total,
                "expansions": expansions,
                "elapsed_s": round(time.perf_counter() - t0, 1),
            }

    try:
        route_params = RoutingParams.from_dict(params_dict)
    except ValueError as exc:
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = f"bad params: {exc}"
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()
        return
    try:
        result, cost_grid, routing_grid = route_nets(
            model=model,
            net_names=net_names,
            protected_nets=protected_nets,
            cost_grid=STATE.cost_grid,
            routing_grid=STATE.routing_grid,
            params=route_params,
            original_pcb_tree=tree,
            extra_power_nets=extra_power,
            progress_cb=_on_progress,
            should_cancel=_should_cancel,
        )
    except ValueError as exc:
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = f"routing rejected: {exc}"
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()
        return
    except Exception as exc:  # noqa: BLE001 — surfaced via poll, never a hang
        import traceback
        traceback.print_exc()
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = f"routing failed: {exc}"
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()
        return

    # Apply to shared state (same writeback as the sync endpoint: rebuild the
    # model from the updated tree so /api/board.svg and /api/nets see copper).
    with STATE._lock:
        STATE.cost_grid = cost_grid
        STATE.routing_grid = routing_grid
        if result.pcb_tree is not None:
            STATE.pcb_tree = result.pcb_tree
            try:
                STATE.model = board_model(result.pcb_tree)
            except Exception:
                STATE.routing_model = model
            else:
                STATE.routing_model = STATE.model
        else:
            STATE.routing_model = model
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
        "stitch_vias_added": result.stitch_vias_added,
        "stitch_tracks_added": result.stitch_tracks_added,
        "gnd_islands_before": result.gnd_islands_before,
        "gnd_islands_after": result.gnd_islands_after,
        "gnd_stripped": result.gnd_stripped,
        "cancelled": result.cancelled,
        "expansions": result.expansions,
        "timings": result.timings,
        "version": version,
    }
    if route_params.debug:
        payload["debug"] = _cost_grid_to_debug(cost_grid, routing_grid)
    with _JOBS_LOCK:
        job["state"] = "done"
        job["result"] = payload
        job["finished"] = time.perf_counter()
        job["progress"] = {
            "stage": "done",
            "net": (net_names or [""])[0],
            "pairs_done": 0,
            "pairs_total": 0,
            "expansions": result.expansions,
            "elapsed_s": round(time.perf_counter() - t0, 1),
        }
    _ROUTING_JOB_LOCK.release()


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
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
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
                                    extra_power_nets=STATE.get_power_override_nets()).encode()
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
                self._send(404, b"no proposal", "text/plain")
                return
            frames = getattr(proposal, "frames", None) or []
            force_frames = getattr(proposal, "force_frames", None) or []
            if not frames:
                self._send(404, b"no frames (re-run with record_frames)", "text/plain")
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
                    "is_decap": fp.uuid in decaps,
                    # Pads with nets drive placement cost (all-pairs repulsion
                    # is O(pads^2)); the UI pre-flight estimate uses this.
                    # Matches _collect_pad_nodes (pad.net_name is not None).
                    "pad_count": sum(1 for pad in fp.pads if pad.net_name is not None),
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
                    "ghost_attractions": [[t, ka] for t, ka in (fp.ghost_attractions or ())],
                    "is_decap": False,  # Ghosts are never decaps (no physics)
                    "pad_count": sum(1 for pad in fp.pads if pad.net_name is not None),
                })
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
            colors = net_colors(model, extra_power)
            # net -> decap refs using the same C*/power+ground rule as
            # placement + svg_render (computed from real footprints;
            # user-marked power counts as power).
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
                    "is_power": is_power_net_effective(net_name, extra_power),
                    "is_ground": is_ground_net(net_name),
                    "power_user": net_name in extra_power,
                    "width_mm": round(resolve_track_width_mm(
                        net_name,
                        net_widths=rp.net_widths,
                        net_netclass=model.net_netclass,
                        net_classes=model.net_classes,
                        default_mm=rp.track_width_mm,
                        power_mm=rp.power_width_mm,
                        extra_power=extra_power,
                    ), 3),
                    "color": colors.get(net_name, "#8a93a0"),
                    "decap_refs": sorted(decap_by_net.get(net_name, [])),
                })
            # Sort: protected first, then by pad count descending
            nets_data.sort(key=lambda n: (not n["protected"], -n["pad_count"]))
            self._send(200, json.dumps({"ok": True, "nets": nets_data}).encode(), "application/json")
            return
        
        # GET /api/routing/params
        if parsed.path == "/api/routing/params":
            params = load_routing_params()
            self._send(200, json.dumps({"ok": True, "params": params.to_dict()}).encode(), "application/json")
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
                proposal = run_placement(
                    model, params, movable_uuids,
                    record_frames=record_frames,
                    frame_stride=frame_stride,
                    max_frames=max_frames,
                    extra_power_nets=STATE.get_power_override_nets(),
                )
            except Exception as exc:
                import traceback
                traceback.print_exc()
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
            # this a static force probe.
            base_params = load_params()
            params = PlacementParams(
                repulsion_k_N_m2=req.get("repulsion_k_N_m2", base_params.repulsion_k_N_m2),
                attraction_k_N=req.get("attraction_k_N", base_params.attraction_k_N),
                max_iterations=0,
                convergence_eps_m=req.get("convergence_eps_m", base_params.convergence_eps_m),
                wall_k_N=req.get("wall_k_N", base_params.wall_k_N),
                wall_area_ref_m2=req.get("wall_area_ref_m2", base_params.wall_area_ref_m2),
                wall_halo_m=req.get("wall_halo_m", base_params.wall_halo_m),
                boundary_k_N=req.get("boundary_k_N", base_params.boundary_k_N),
                ghost_k_N=req.get("ghost_k_N", base_params.ghost_k_N),
                damping_Ns_m=req.get("damping_Ns_m", base_params.damping_Ns_m),
                friction_N=req.get("friction_N", base_params.friction_N),
                dt_s=req.get("dt_s", base_params.dt_s),
                vmax_m_s=req.get("vmax_m_s", base_params.vmax_m_s),
                power_attraction_scale=req.get("power_attraction_scale", base_params.power_attraction_scale),
                decap_boost=req.get("decap_boost", base_params.decap_boost),
                include_power_nets=req.get("include_power_nets", base_params.include_power_nets),
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
                proposal = run_placement(
                    model, params, movable_uuids,
                    extra_power_nets=STATE.get_power_override_nets(),
                )
            except Exception as exc:
                import traceback
                traceback.print_exc()
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
        
        if parsed.path == "/api/nets/protected" and self.command == "GET":
            protected = STATE.get_protected_nets()
            self._send(200, json.dumps({"ok": True, "protected": sorted(protected)}).encode(), "application/json")
            return
        
        # POST /api/nets/protected - explicitly mark/unmark a protected net.
        # (Fixed 2026-10-12: previously `protected: true` toggled instead
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
        
        # ==================== Routing API ====================

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
            params = RoutingParams.from_dict(req)
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
            if not _ROUTING_JOB_LOCK.acquire(blocking=False):
                self._send(409, b"another routing job is running", "text/plain")
                return
            job_id = uuidlib.uuid4().hex[:12]
            with _JOBS_LOCK:
                _prune_jobs_locked()
                _JOBS[job_id] = {
                    "job_id": job_id,
                    "state": "queued",
                    "net_names": [n for n in net_names if isinstance(n, str) and n],
                    "params_dict": req.get("params", {}),
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
            worker.start()
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
                    extra_power_nets=STATE.get_power_override_nets(),
                )
            except ValueError as exc:
                # Client error (e.g. grid too large for the board): 400 with
                # an actionable message, not a 500.
                self._send(400, f"routing rejected: {exc}".encode(), "text/plain")
                return
            except Exception as exc:
                import traceback
                traceback.print_exc()
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
            
            payload = {
                "ok": True,
                "tracks_added": result.tracks_added,
                "vias_added": result.vias_added,
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
                "version": version,
            }
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
            vias_after = _Counter(v.net_name for v in new_model.vias if v.net_name)
            ripped: dict = {}
            for net in rippable:
                dt = tracks_before.get(net, 0) - tracks_after.get(net, 0)
                dv = vias_before.get(net, 0) - vias_after.get(net, 0)
                if dt or dv:
                    ripped[net] = {"tracks": dt, "vias": dv}
            payload = {
                "ok": True,
                "ripped": ripped,
                "skipped_protected": skipped_protected,
                "version": version,
            }
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
