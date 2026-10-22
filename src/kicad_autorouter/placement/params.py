"""Placement-dynamics parameters (SI: newtons, meters, seconds).

Geometry inside the solver stays millimeter-native (shapely/Kabsch are
unit-agnostic linear algebra); every force law converts to SI at evaluation
and the integrator runs fully in m/kg/s. Normative formulas:
``docs/si_force_laws.md``.

Timestep fields (dt_s, dt_min_s, dt_max_s) are solver internals: they are
kept in the dataclass for round-trip compat with placement.json and from_dict,
but they are not exposed as UI sliders. The adaptive-dt guard manages them
internally.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from typing import Dict

# Trajectory-frame viz precision: 1 micron / 0.01 deg is far below the
# convergence epsilon (~0.01mm) and invisible on SVG, but cuts JSON payload
# ~30% vs full float repr.
FRAME_XY_DECIMALS = 3
FRAME_ANGLE_DECIMALS = 2
FRAME_DEFAULT_MAX = 100

# Angular deadband (rad/step): DISABLED (0.0). A 0.1° deadband parks
# micro-rotations that wedged pairs need to work themselves apart. Idle-
# orientation burn is bounded instead by the stall rule (no window-mean
# progress stops "stalled"). Deliberately NOT a PlacementParams slider.
MIN_ROT_STEP_RAD = 0.0

# Stall-rule tolerance multiplier (dimensionless): the stall check compares
# the window-averaged pose shift against STALL_MEAN_EPS_MULT * eps, not eps
# itself. Steady chatter holds window means still to ~1.5 µm while single
# steps swing ~80 µm and real transit moves the mean ~6.6 µm; 4x sits between
# chatter noise and real progress. Deliberately NOT a PlacementParams slider.
STALL_MEAN_EPS_MULT = 4.0


@dataclass(frozen=True)
class PlacementParams:
    """SI placement-dynamics parameters (v2 set; newtons, meters, seconds).

    Legacy v1 fields are gone on purpose — calibration
    (``scripts/calibrate_si.py``) learns these ranges empirically.
    """

    # Step budget; 0 = static force probe (no integration steps, returns
    # initial forces with empty deltas — used by the server force overlay).
    max_iterations: int = 1000
    # Pair repulsion strength (Coulomb-like k/d^2, d in meters).
    repulsion_k_N_m2: float = 2e-9
    # Net attraction spring pull (constant magnitude, newtons).
    attraction_k_N: float = 8e-3
    # Courtyard contact strength (newtons; elastic push-out on collision).
    wall_k_N: float = 8e-2
    # Near-miss halo reach (meters). Calibrated 6mm (sweep_wall_hardening.py).
    wall_halo_m: float = 0.006
    # Wall-law exponent (dimensionless, default 2 = quadratic).
    # Quadratic reduces overlaps on real boards (sweep_wall_hardening.py).
    wall_exponent: float = 2.0
    # Deep-penetration cap (mm of penetration depth, 0 = uncapped).
    wall_cap_depth_mm: float = 0.0
    # Velocity blending alpha: fraction of old velocity to retain after
    # rigid projection (0 = fully re-derive from displacement, 1 = keep old).
    # Default 0.7 provides momentum persistence to nudge past wedges.
    momentum_alpha: float = 0.7
    # Tangential assist coefficient: fraction of normal force applied along
    # the tangential direction at contact to help parts slide past each other.
    # 0 = disabled, >0 adds force along the tangential (MTV-perp) axis.
    # Typical values 0.1-0.3. This helps wedged parts slide along walls.
    contact_tangent_gamma: float = 0.15
    # Ghost attraction scale (newtons).
    ghost_k_N: float = 5e-5
    # Viscous damping coefficient (N*s/m).
    damping_Ns_m: float = 0.12
    # Dry-friction deadband (N of total footprint force).
    friction_N: float = 1e-6
    # Timestep (s); adaptive guard halves on energy rise, regrows on fall.
    # Solver internal — not a UI slider.
    dt_s: float = 1e-3
    # Timestep floor (s) and ceiling (s) for the adaptive guard. Solver
    # internals — not UI sliders.
    dt_min_s: float = (1/20) * (1/1000) #5% of a millisecond
    dt_max_s: float = (2/1000) #2 millisecond
    # Speed ceiling (m/s). Enforced on velocities AND, via the rigid-step
    # clamp, on post-projection body motion (translation <= vmax*dt,
    # rotation <= 5 deg/step).
    vmax_m_s: float = 1.0
    # Settled threshold: max pad displacement per step (meters).
    # Calibrated 20 µm (si_force_laws.md): tighter eps fake-settles under
    # contact chatter; this is the honest-floor where residual gate still catches.
    convergence_eps_m: float = 2e-5
    # Stall window (iterations): no NET progress of the window-averaged
    # pose over the window stops the run as "stalled" instead of burning
    # budget. A stalled run sits in place with unserved forces
    # (wedged/chattering with no resolvable drift) — headless callers must
    # treat it like budget (needs attention: move the neighbors), never
    # like converged. 0 disables.
    stall_window: int = 100
    # Footprint mass fallback slope (kg/m^2 of courtyard area).
    mass_density_kg_m2: float = 3.0
    # Minimum part mass (kg).
    mass_floor_kg: float = 1e-9
    # Power-rail attraction trim (dimensionless).
    power_attraction_scale: float = 1.0
    # Decoupling-cap edge multiplier (dimensionless).
    decap_boost: float = 4.0
    # Kill-switch for power participation.
    include_power_nets: bool = True
    # Courtyard-overlap area (mm^2) above which a pair is illegal.
    overlap_tolerance_mm2: float = 0.1
    # Board-wall protrusion depth (mm) above which a part is illegal.
    wall_tolerance_mm: float = 0.1
    # Debug instrumentation: when True, run_placement attaches a debug_trace
    # dict to the proposal with per-iteration per-pad forces/positions and
    # depen-pass diagnostics. Off by default (zero overhead in production).
    debug: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlacementParams":
        """Build params from a JSON-style dict; missing keys take defaults and
        unknown keys are ignored (so the UI may POST partial param sets).

        ``dt_min_s``/``dt_max_s`` are solver-internal timestep guards; they are
        never exposed on the UI and are deliberately not user-controllable, so
        any values present in ``data`` (e.g. a stale placement.json round-trip)
        are dropped here and the dataclass defaults take over. This keeps
        lowering ``dt_s`` from ever failing validation against a stale
        ``dt_min_s``.
        """
        known = {f.name for f in fields(cls)}
        params = cls(**{
            name: data[name] for name in known
            if name in data and name not in ("dt_min_s", "dt_max_s")
        })
        _validate(params)
        return params


def _validate(params: PlacementParams) -> None:
    numeric = (
        "repulsion_k_N_m2",
        "attraction_k_N",
        "wall_k_N",
        "wall_halo_m",
        "ghost_k_N",
        "damping_Ns_m",
        "friction_N",
        "dt_s",
        "dt_min_s",
        "dt_max_s",
        "vmax_m_s",
        "convergence_eps_m",
        "mass_density_kg_m2",
        "mass_floor_kg",
        "power_attraction_scale",
        "decap_boost",
        "wall_exponent",
        "wall_cap_depth_mm",
        "overlap_tolerance_mm2",
        "wall_tolerance_mm",
    )
    for name in numeric:
        value = getattr(params, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if value < 0:
            raise ValueError(f"{name} must be >= 0")
    for name in ("dt_s", "dt_min_s", "dt_max_s", "vmax_m_s", "wall_halo_m"):
        if getattr(params, name) <= 0:
            raise ValueError(f"{name} must be > 0")
    if params.dt_min_s > params.dt_s:
        raise ValueError("dt_min_s must be <= dt_s")
    if params.dt_s > params.dt_max_s:
        raise ValueError("dt_s must be <= dt_max_s")
    if params.power_attraction_scale < 0:
        raise ValueError("power_attraction_scale must be >= 0")
    if params.wall_exponent < 1.0:
        raise ValueError("wall_exponent must be >= 1.0 (p < 1 is singular at the halo edge)")
    if not (0.0 <= params.momentum_alpha <= 1.0):
        raise ValueError("momentum_alpha must be in [0.0, 1.0]")
    if params.contact_tangent_gamma < 0.0:
        raise ValueError("contact_tangent_gamma must be >= 0.0")
    if params.decap_boost < 0:
        raise ValueError("decap_boost must be >= 0")
    if not isinstance(params.include_power_nets, bool):
        raise ValueError("include_power_nets must be a boolean")
    if not isinstance(params.debug, bool):
        raise ValueError("debug must be a boolean")
    if isinstance(params.max_iterations, bool) or not isinstance(
        params.max_iterations, int
    ):
        raise ValueError("max_iterations must be an integer")
    if params.max_iterations < 0:
        raise ValueError("max_iterations must be >= 0 (0 = static force probe: no steps, initial forces only)")
    if isinstance(params.stall_window, bool) or not isinstance(
        params.stall_window, int
    ):
        raise ValueError("stall_window must be an integer")
    if params.stall_window < 0:
        raise ValueError("stall_window must be >= 0 (0 = disabled)")
