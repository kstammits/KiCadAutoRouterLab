"""In-memory board/pair state for the workflow UI server.

Extracted from server.py: BoardState (the loaded board pair, proposal, undo
stack, routing caches) plus the STATE singleton. server.py re-exports STATE
so existing imports keep working.
"""

import threading
import time
from typing import Any, Dict, Optional, Set, Tuple

import numpy as np

from kicad_autorouter.board_model import (
    board_model,
    commit_placement,
    seed_power_nets_from_netclasses,
)
from kicad_autorouter.ghost import make_ghost_attractor, make_ghost_wall
from kicad_autorouter.io import (
    add_ghost_to_tree,
    nudge_footprint_by_uuid,
    remove_ghost_from_tree,
    resize_ghost_in_tree,
    rip_up_nets,
)
from kicad_autorouter.routing.power import identify_power_nets
from kicad_autorouter.sexpr import parse
from kicad_autorouter.strategy import infer_mode, mode_warning


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
        # Selection/pin are view state: they bump selection_version only,
        # never the board version (which drives heavy SVG re-injects in the
        # client's poll loop). /api/state exposes both.
        self.selection_version = 0
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
        # Inlet anchors for impedance screening (user-picked, in-memory only):
        # gnd_inlets is a list of {"ref","pad"} (parallel feeds welcome);
        # power_inlets maps net -> list of anchors; bridges join mated
        # inter-panel pairs: {"a": {"ref","pad"}, "b": {...}, "r_mohm": 20.0}.
        self.gnd_inlets: list = []
        self.power_inlets: Dict[str, list] = {}
        self.bridges: list = []
        # Last routing outcome per net (net -> reason), for the nets-table
        # failure badge. Overwritten by every routing/refine run; cleared
        # on board load/reset (stale refs must not linger).
        self.last_failed_reasons: Dict[str, str] = {}
        self.last_failed_version: Optional[int] = None
        # Board mode switch (two-layer | four-layer): per-board server
        # memory, inferred from the stackup on load, user-overridable.
        # Presets apply as an in-memory overlay (never written to disk).
        self.mode: Optional[str] = None

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
                # Inlet anchors reference pads on this board; a new board clears them.
                self.gnd_inlets = []
                self.power_inlets = {}
                self.bridges = []
                self.last_failed_reasons = {}
                self.last_failed_version = None
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
                # Infer the board mode from the stackup (user-overridable).
                self.mode = infer_mode(self.model.copper_layers) if self.model else None
            self.version += 1
            self.selection_version += 1
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

    def _get_copied(self, field, coerce):
        """Return ``coerce(getattr(self, field))`` under lock (copy-out getter)."""
        with self._lock:
            return coerce(getattr(self, field))

    def _set_versioned(self, field, value, coerce, counter="version"):
        """Store ``coerce(value)``, bump ``counter``, return the new count."""
        with self._lock:
            setattr(self, field, coerce(value))
            new = getattr(self, counter) + 1
            setattr(self, counter, new)
            return new

    def set_proposal(self, proposal) -> int:
        return self._set_versioned("proposal", proposal, lambda v: v)

    def get_pinned_uuids(self) -> set[str]:
        return self._get_copied("pinned_uuids", set.copy)

    def get_selected_uuids(self) -> set[str]:
        return self._get_copied("selected_uuids", set.copy)

    def get_forces(self) -> Dict[str, Tuple[float, float]]:
        return self._get_copied("forces", dict.copy)

    def set_forces(self, forces: Dict[str, Tuple[float, float]]) -> int:
        return self._set_versioned("forces", forces, dict.copy)

    def get_force_components(self) -> Dict[str, Dict[str, Tuple[float, float]]]:
        return self._get_copied(
            "force_components", lambda d: {u: dict(c) for u, c in d.items()})

    def set_force_components(
        self, components: Dict[str, Dict[str, Tuple[float, float]]]
    ) -> int:
        return self._set_versioned(
            "force_components", components,
            lambda d: {u: dict(c) for u, c in d.items()})

    def get_drc_violations(self) -> list[dict]:
        return self._get_copied("drc_violations", list.copy)

    def set_drc_violations(self, violations: list[dict]) -> int:
        return self._set_versioned("drc_violations", violations, list.copy)

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

    def get_selection_version(self) -> int:
        with self._lock:
            return self.selection_version

    def set_pinned_uuids(self, uuids: set[str]) -> int:
        return self._set_versioned(
            "pinned_uuids", uuids, set.copy, "selection_version")

    def set_selected_uuids(self, uuids: set[str]) -> int:
        return self._set_versioned(
            "selected_uuids", uuids, set.copy, "selection_version")

    def replace_selection(self, uuids: set[str]) -> int:
        """Replace the whole selection set (bulk ops, single-select)."""
        return self._set_versioned(
            "selected_uuids", uuids, set, "selection_version")

    def set_selected(self, uuid: str, selected: bool) -> int:
        """Idempotent single-uuid set (no toggle quirk for optimistic UI)."""
        with self._lock:
            if selected:
                self.selected_uuids.add(uuid)
            else:
                self.selected_uuids.discard(uuid)
            self.selection_version += 1
            return self.selection_version

    def toggle_pinned(self, uuid: str) -> int:
        with self._lock:
            if uuid in self.pinned_uuids:
                self.pinned_uuids.remove(uuid)
            else:
                self.pinned_uuids.add(uuid)
            self.selection_version += 1
            return self.selection_version

    def toggle_selected(self, uuid: str, selected: bool | None = None) -> int:
        with self._lock:
            if selected is None:
                if uuid in self.selected_uuids:
                    self.selected_uuids.remove(uuid)
                else:
                    self.selected_uuids.add(uuid)
            elif selected:
                self.selected_uuids.add(uuid)
            else:
                self.selected_uuids.discard(uuid)
            self.selection_version += 1
            return self.selection_version

    def clear_selection(self) -> int:
        with self._lock:
            self.selected_uuids.clear()
            self.selection_version += 1
            return self.selection_version

    def select_all_unlocked(self, model) -> int:
        """Select all unlocked, non-pinned footprints."""
        with self._lock:
            self.selected_uuids = {
                fp.uuid for fp in model.footprints
                if fp.uuid and not fp.locked and fp.uuid not in self.pinned_uuids
            }
            self.selection_version += 1
            return self.selection_version

    # ==================== Protected Nets & Routing State ====================

    def get_protected_nets(self) -> set[str]:
        """Return copy of protected nets set."""
        return self._get_copied("protected_nets", set.copy)

    def set_protected_nets(self, nets: set[str]) -> int:
        """Replace the entire protected nets set."""
        return self._set_versioned("protected_nets", nets, set)

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
        return self._get_copied("power_override_nets", set)

    def set_power_override_net(self, net_name: str, is_power: bool) -> int:
        """Explicitly mark/unmark a net as power (no toggle quirk)."""
        with self._lock:
            if is_power:
                self.power_override_nets.add(net_name)
            else:
                self.power_override_nets.discard(net_name)
            self.version += 1
            return self.version

    def get_inlets(self) -> dict:
        """Return copies of the inlet anchors (no version bump; derived state)."""
        with self._lock:
            return {
                "gnd": list(self.gnd_inlets),
                "gnd_list": list(self.gnd_inlets),
                "power": {n: list(a) for n, a in self.power_inlets.items()},
                "bridges": list(self.bridges),
            }

    def set_gnd_inlets(self, anchors: list) -> int:
        """Replace the GND inlet list (one anchor per panel region)."""
        with self._lock:
            self.gnd_inlets = [dict(a) for a in anchors]
            self.version += 1
            return self.version

    def add_gnd_inlet(self, ref: str, pad: str) -> int:
        """Append a GND inlet anchor (deduped)."""
        with self._lock:
            anchor = {"ref": ref, "pad": pad}
            if anchor not in self.gnd_inlets:
                self.gnd_inlets.append(anchor)
            self.version += 1
            return self.version

    def set_power_inlets(self, net_name: str, anchors: list) -> int:
        """Replace one power rail's inlet list (empty clears it)."""
        with self._lock:
            if anchors:
                self.power_inlets[net_name] = [dict(a) for a in anchors]
            else:
                self.power_inlets.pop(net_name, None)
            self.version += 1
            return self.version

    def set_bridges(self, bridges: list) -> int:
        """Replace the mated-pair bridge list."""
        with self._lock:
            self.bridges = [dict(b) for b in bridges]
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

    # ==================== Board mode ====================

    def set_mode(self, mode: str) -> None:
        """Store a validated board mode (no version bump: derived UI state)."""
        with self._lock:
            self.mode = mode

    def mode_status(self) -> dict:
        """{mode, inferred, warning} — warn-never-block stackup mismatch info."""
        with self._lock:
            mode = self.mode
            layers = tuple(self.model.copper_layers) if self.model is not None else ()
        return {
            "mode": mode,
            "inferred": infer_mode(layers),
            "warning": mode_warning(mode, layers),
        }

    def reset_routing_state(self) -> int:
        """Clear routing state (cost_grid, routing_grid, routing_model)."""
        with self._lock:
            self.cost_grid = None
            self.routing_grid = None
            self.routing_model = None
            self.version += 1
            return self.version

    def set_last_failed(self, reasons: dict) -> None:
        """Record the last routing run's per-net failure reasons.

        Overwrites the previous run (fixed nets drop off). No version
        bump: derived display state, read alongside /api/nets.
        """
        with self._lock:
            self.last_failed_reasons = {
                str(k): str(v) for k, v in (reasons or {}).items()
            }
            self.last_failed_version = self.version

    def get_last_failed(self) -> tuple:
        """Return (reasons copy, version stamp) of the last routing run."""
        with self._lock:
            return dict(self.last_failed_reasons), self.last_failed_version

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
            self.selection_version += 1
            return self.version

    def reset_to_original(self) -> int:
        """Reset to the originally loaded board (mode re-inferred)."""
        with self._lock:
            if self.original_pcb_tree is not None:
                self.model = board_model(self.original_pcb_tree)
                self.pcb_tree = self.original_pcb_tree
                self.proposal = None
                self.selected_uuids.clear()
                self.selection_version += 1
                self.undo_stack.clear()
                self._last_nudge_at = 0.0
                self._last_nudge_keys = frozenset()
                # Reset routing state
                self.cost_grid = None
                self.routing_grid = None
                self.routing_model = None
                self.last_failed_reasons = {}
                self.last_failed_version = None
                # Restore board version and warning from original
                self.board_version = self.model.version if self.model else ""
                self.version_warning = self.model.version_warning if self.model else None
                # The restored bytes may carry a different stackup than the
                # edited board; re-infer so mode matches the served content.
                # Undo keeps the user's mode (single-step revert, same board).
                self.mode = infer_mode(
                    self.model.copper_layers) if self.model else None
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
            arcs_before = _Counter(a.net_name for a in self.model.arcs if a.net_name)
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
            arcs_after = _Counter(a.net_name for a in self.model.arcs if a.net_name)
            vias_after = _Counter(v.net_name for v in self.model.vias if v.net_name)
            ripped: dict = {}
            for net in sorted(affected_nets - protected_nets):
                dt = tracks_before.get(net, 0) - tracks_after.get(net, 0)
                da = arcs_before.get(net, 0) - arcs_after.get(net, 0)
                dv = vias_before.get(net, 0) - vias_after.get(net, 0)
                if dt or da or dv:
                    ripped[net] = {"tracks": dt, "arcs": da, "vias": dv}
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
        for name, val in (("x_mm", x_mm), ("y_mm", y_mm)):
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(f"{name} must be a finite number, got {val!r}")
        if kind == "wall":
            for name, val in (("w_mm", w_mm), ("h_mm", h_mm)):
                if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                    raise ValueError(f"{name} must be a positive finite number, got {val!r}")
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
                try:
                    ka_f = float(ka)
                except (TypeError, ValueError):
                    raise ValueError(f"ka must be numeric, got {ka!r}")
                if not math.isfinite(ka_f):
                    raise ValueError(f"ka must be finite, got {ka!r}")
                fp = make_ghost_attractor(ref, x_mm, y_mm, target_ref, ka_f, size_mm=size_f)
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
