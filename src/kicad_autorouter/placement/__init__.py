"""Placement dynamics package (SI: newtons, meters, seconds).

Public API (unchanged from the old placement.py module):
PlacementParams, PlacementProposal, run_placement.

Submodules: params, model, rigid, contact, force, integrate, runner.

The private-helper re-exports below exist for backward compatibility:
tests and scripts import names like ``_wall_mag`` or ``_collect_pad_nodes``
directly from ``kicad_autorouter.placement``. New code should import from
the owning submodule instead (e.g. ``...placement.contact``).
"""

from .contact import (
    _contact_weights,
    _courtyard_outline_polygon,
    _footprint_courtyard_polygon,
    _wall_mag,
)
from .integrate import _thin_frames
from .model import (
    _build_net_edges_pad_level,
    _collect_pad_nodes,
    _find_decap_pad_indices,
)
from .params import (
    FRAME_ANGLE_DECIMALS,
    FRAME_XY_DECIMALS,
    MIN_ROT_STEP_RAD,
    STALL_MEAN_EPS_MULT,
    PlacementParams,
)
from .rigid import _clamp_rigid_step, _rigid_pose_vertices
from .runner import PlacementProposal, _run_region_simulation, run_placement
from .legality import LegalityReport, check_legality

__all__ = [
    "PlacementParams",
    "PlacementProposal",
    "run_placement",
    "LegalityReport",
    "check_legality",
    # Backward-compat re-exports (import from submodules in new code):
    "FRAME_ANGLE_DECIMALS",
    "FRAME_XY_DECIMALS",
    "MIN_ROT_STEP_RAD",
    "STALL_MEAN_EPS_MULT",
    "_build_net_edges_pad_level",
    "_clamp_rigid_step",
    "_collect_pad_nodes",
    "_contact_weights",
    "_courtyard_outline_polygon",
    "_find_decap_pad_indices",
    "_footprint_courtyard_polygon",
    "_rigid_pose_vertices",
    "_run_region_simulation",
    "_thin_frames",
    "_wall_mag",
]
