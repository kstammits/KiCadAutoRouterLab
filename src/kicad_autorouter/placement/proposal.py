"""PlacementProposal dataclass: result of a placement run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class PlacementProposal:
    """Result of a placement run: per-footprint deltas keyed by UUID + diagnostics.

        Deltas are (dx_mm, dy_mm, dangle_deg) for each movable footprint.
    Forces are (Fx, Fy) in arbitrary units for each movable footprint.

    Frames (opt-in via run_placement(record_frames=True)) hold per-sampled-
    iteration (dx, dy, da) deltas vs the committed start positions for the
    SVG motion player; already rounded + downsampled to max_frames.
    frames[0] is always the committed starting pose (all-zero deltas =
    completed iteration 0); frames[k] is the pose after k*stride completed
    iterations, except the last frame which is always the final pose after
    ``iterations`` completed iterations.

    force_frames (parallel to frames): per-sampled-iteration force breakdown
    for each movable footprint. Each frame is {uuid: {cause: (fx, fy)}} with
    causes in {"repulsion", "attraction", "courtyard", "ghost",
    "tangential"} (courtyard includes the board wall; tangential is the
    velocity-dependent sliding assist — its sign follows relative
    tangential motion, so static probes and dynamic runs can differ).
    Values rounded to 3 significant figures for compact JSON.

    stop_reason reports how the run ended: "converged" (per-step displacement
    below eps AND residual forces below the settle threshold — genuinely at
    rest), "stalled" (the window-averaged pose shows no net progress over a
    full stall window while forces remain unserved — wedged or chattering
    in place; move the neighbors, like budget it needs attention, never
    rest), "budget" (max_iterations reached with work still
    outstanding — headless callers must treat this as needs-attention, not
    settled), or "empty" (nothing movable / nothing to integrate). residual_max_force_N
    is the largest movable-footprint net-force norm (newtons, post-friction)
    on the final iteration: near zero at true rest, large when the run stopped
    with pushes still unserved (e.g. a wedged overlap). Net — not per-pad:
    rigid bodies in contact carry canceling per-pad loads that never vanish.
    depenetrated flags a post-loop geometric eject: the final pose moved
    after the dynamics stopped, so "converged" means settled-then-pushed.
    skipped_uuidless counts footprints (real + ghost) with no uuid: pad
    collection, region assignment, deltas, and legality all key off uuid,
    so these parts are exempt from physics. Normally zero; nonzero means
    the board (or a writeback path) dropped uuids — surfaced, not silent.

    label_deltas (orbit mode only) maps footprint UUID to the label's
    footprint-local offset (dx_mm, dy_mm) from its filed anchor. Empty in
    follow mode. ``apply_deltas`` and the ``io`` writeback consume it to
    move ``fp_text (at)``; trajectory frames stay footprint-only (labels
    ride the final pose in replay — documented v1 limitation).
    """

    deltas: Dict[str, Tuple[float, float, float]]
    iterations: int
    final_max_disp_mm: float
    elapsed_s: float
    params: dict
    forces: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    force_components: Dict[str, Dict[str, Tuple[float, float]]] = field(
        default_factory=dict
    )
    frames: List[Dict[str, Tuple[float, float, float]]] = field(default_factory=list)
    force_frames: List[Dict[str, Dict[str, Tuple[float, float]]]] = field(
        default_factory=list
    )
    label_deltas: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    frame_stride: int = 1
    stop_reason: str = "budget"
    residual_max_force_N: float = 0.0
    # True when the post-loop de-penetration eject moved parts: the final
    # pose is geometric ejection, not pure force settle, and
    # residual_max_force_N was re-read at the ejected pose. A "converged"
    # run with this set settled its dynamics, then got pushed — headless
    # callers deciding "run again" should weigh it like attention, not rest.
    depenetrated: bool = False
    # Footprints (real + ghost) skipped for lack of a uuid. See class doc.
    skipped_uuidless: int = 0
    # Legality fields (post-hoc geometric check on final positions)
    legality_is_legal: bool = True
    overlapping_pairs: Tuple[Tuple[str, str, float], ...] = ()
    wall_violations: Tuple[Tuple[str, float], ...] = ()
    max_pair_overlap_mm2: float = 0.0
    max_wall_penetration_mm: float = 0.0
    exempt_pairs: Tuple[Tuple[str, str], ...] = ()
    # Refs whose declared courtyard outline is drafting debris (e.g. Fab
    # layers carrying only pin-marker dots): collision, mass, and the SVG
    # all fall back to the pad bounding box for these parts. Surfaced so a
    # bad library footprint is visible instead of silently body-less.
    degenerate_bodies: Tuple[str, ...] = ()
    # Debug instrumentation (only populated when params.debug is True).
    # Keys: "per_iteration" (list of per-iteration snapshots), "depen"
    # (before/after overlap + steps applied for the depenetration pass).
    debug_trace: dict = field(default_factory=dict)
