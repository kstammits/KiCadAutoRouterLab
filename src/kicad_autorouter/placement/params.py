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

# Angular deadband (rad/step): per-step rotations below this are numerical
# / gradient creep, not torque response — drop them so orientation doesn't
# random-walk over long runs. Deliberately NOT a PlacementParams slider:
# it is a noise floor, not a tuning knob.
MIN_ROT_STEP_RAD = math.radians(0.1)


@dataclass(frozen=True)
class PlacementParams:
    """SI placement-dynamics parameters (v2 set; newtons, meters, seconds).

    Legacy v1 fields are gone on purpose — calibration
    (``scripts/calibrate_si.py``) learns these ranges empirically.
    """

    max_iterations: int = 1000
    # Pair repulsion strength (Coulomb-like k/d^2, d in meters).
    repulsion_k_N_m2: float = 2e-9
    # Net attraction spring pull (constant magnitude, newtons).
    attraction_k_N: float = 8e-3
    # Courtyard contact strength (newtons; elastic push-out on collision).
    wall_k_N: float = 8e-2
    # Near-miss halo reach (meters).
    wall_halo_m: float = 0.006
    # Wall-law exponent (dimensionless, default 1 = linear).
    wall_exponent: float = 1.0
    # Deep-penetration cap (mm of penetration depth, 0 = uncapped).
    wall_cap_depth_mm: float = 0.0
    # Boundary repulsion strength (newtons).
    boundary_k_N: float = 1e-2
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
    dt_min_s: float = 5e-4
    dt_max_s: float = 5e-3
    # Speed ceiling (m/s). Enforced on velocities AND, via the rigid-step
    # clamp, on post-projection body motion (translation <= vmax*dt,
    # rotation <= 5 deg/step).
    vmax_m_s: float = 1.0
    # Settled threshold: max pad displacement per step (meters).
    convergence_eps_m: float = 2e-5
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlacementParams":
        """Build params from a JSON-style dict; missing keys take defaults and
        unknown keys are ignored (forward compatibility with newer param sets,
        and silent drop of legacy v1 keys)."""
        known = {f.name for f in fields(cls)}
        params = cls(**{name: data[name] for name in known if name in data})
        _validate(params)
        return params


def _validate(params: PlacementParams) -> None:
    numeric = (
        "repulsion_k_N_m2",
        "attraction_k_N",
        "wall_k_N",
        "wall_halo_m",
        "boundary_k_N",
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
    if params.decap_boost < 0:
        raise ValueError("decap_boost must be >= 0")
    if not isinstance(params.include_power_nets, bool):
        raise ValueError("include_power_nets must be a boolean")
    if isinstance(params.max_iterations, bool) or not isinstance(
        params.max_iterations, int
    ):
        raise ValueError("max_iterations must be an integer")
    if params.max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
