"""Placement dynamics package (SI: newtons, meters, seconds).

Public API:
PlacementParams, PlacementProposal, run_placement.

Submodules: params, model, rigid, contact, force, legality, runner, proposal, depenetrate, region.

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
    courtyard_is_degenerate,
)
from .runner import PlacementProposal, _run_region_simulation, _thin_frames, run_placement
from .model import (
    _build_net_edges_pad_level,
    _collect_pad_nodes,
    _find_decap_chip_links,
    _find_decap_pad_indices,
    collect_pad_nodes,
    find_decap_chip_links,
)
from .params import (
    FRAME_ANGLE_DECIMALS,
    FRAME_XY_DECIMALS,
    MIN_ROT_STEP_RAD,
    STALL_MEAN_EPS_MULT,
    PlacementParams,
)
from .rigid import _clamp_rigid_step, _rigid_pose_vertices
from .legality import LegalityReport, check_legality
from .refine import refine_placement, refine_placement_with_report

__all__ = [
    "PlacementParams",
    "PlacementProposal",
    "run_placement",
    "LegalityReport",
    "check_legality",
    "refine_placement",
    "refine_placement_with_report",
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
    "_find_decap_chip_links",
    "_find_decap_pad_indices",
    "_footprint_courtyard_polygon",
    "courtyard_is_degenerate",
    "find_decap_chip_links",
    "collect_pad_nodes",
    "_rigid_pose_vertices",
    "_run_region_simulation",
    "_thin_frames",
    "_wall_mag",
]
