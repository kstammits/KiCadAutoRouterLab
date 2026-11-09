"""Placement runner: region grouping, run_placement orchestrator, frame merging.

Per-region force simulation lives in :mod:`placement.region`; the final
de-penetration eject in :mod:`placement.depenetrate`; the result dataclass in
:mod:`placement.proposal`. This module is a thin orchestrator: validate params,
collect pads, group by region, delegate to ``region._run_region_simulation``,
merge per-region frame lists, run post-hoc legality, and assemble the
:class:`placement.proposal.PlacementProposal`.

Deltas are keyed by footprint UUID (dx_mm, dy_mm, dangle_deg) because refs may
be duplicated or missing; locked footprints never appear in ``deltas`` (they
are fixed anchors).
"""

from __future__ import annotations

import math
import time
from collections.abc import Collection
from typing import Dict, List, Optional, Set, Tuple, TypeVar

from ..board_model import BoardModel, BoardRegion
from .contact import _board_bbox, courtyard_is_degenerate
from .legality import LegalityReport, check_legality
from .model import _collect_pad_nodes
from .params import FRAME_DEFAULT_MAX, PlacementParams, _validate
from .proposal import PlacementProposal
from .region import _run_region_simulation  # noqa: F401  re-exported for back-compat

V = TypeVar("V")


def _merge_region_lists(
    per_region: List[List[Dict[str, V]]],
) -> List[Dict[str, V]]:
    """Merge per-region frame lists into global frames.

    Regions converge independently, so lists differ in length; shorter
    regions hold their last pose (parts at rest) while longer ones keep
    moving. Union of uuids per index.
    """
    if not per_region:
        return []
    n = max(len(f) for f in per_region)
    merged: List[Dict[str, V]] = []
    for k in range(n):
        frame: Dict[str, V] = {}
        for frames in per_region:
            if not frames:
                continue
            frame.update(frames[min(k, len(frames) - 1)])
        merged.append(frame)
    return merged


def _thin_frames(
    frames: List[Dict[str, Tuple[float, float, float]]], max_frames: int
) -> List[Dict[str, Tuple[float, float, float]]]:
    """Uniformly downsample frames to at most max_frames, always keeping last.

    Deterministic (round-half-away via int(i * (n-1) / (m-1))); no-ops when
    already within budget.
    """
    n = len(frames)
    if max_frames < 1:
        return []
    if n <= max_frames:
        return frames
    if max_frames == 1:
        return [frames[-1]]
    return [
        frames[int(i * (n - 1) / (max_frames - 1))] for i in range(max_frames - 1)
    ] + [frames[-1]]


def run_placement(
    model: BoardModel,
    params: PlacementParams,
    movable_uuids: Optional[Set[str]] = None,
    record_frames: bool = False,
    frame_stride: int = 1,
    max_frames: int = FRAME_DEFAULT_MAX,
    extra_power_nets: Collection[str] = (),
    freeze_bodies: bool = False,
) -> PlacementProposal:
    """Run per-pad force-spring placement for ``model`` under ``params``.

    Supports multi-board region simulation: footprints are grouped by board
    outline region (from Edge.Cuts) and each region is simulated independently
    with its own bounding box and fixed per-iteration step budget.

    ``params`` is re-validated here because direct ``PlacementParams(...)``
    construction bypasses the ``from_dict`` check (only the API path
    validates); fail fast instead of dividing by zero mid-run.

    With ``freeze_bodies``, footprints never move while labels still orbit
    to equilibrium — a label-settle pass. Convergence then means the labels
    went quiet, so the legality downgrade (which judges part poses) is
    skipped: pre-existing part overlaps are not the settle pass's verdict.
    """
    _validate(params)
    start = time.perf_counter()
    # Uuid-less footprints are exempt from physics everywhere downstream
    # (pad collection, regions, deltas, legality all key off uuid): count
    # them once so the proposal surfaces what the solver never saw.
    skipped_uuidless = sum(
        1 for fp in (*model.footprints, *model.ghost_footprints)
        if not fp.uuid
    )
    _eff_stride = max(1, int(frame_stride or 1))
    if record_frames and max_frames >= 1 and params.max_iterations > max_frames:
        _eff_stride = max(
            _eff_stride, math.ceil(params.max_iterations / max_frames)
        )

    # Collect pad nodes
    pads, pad_to_fp_idx, fp_uuid_to_pad_indices, global_pad_idx_to_local, pad_is_ghost = _collect_pad_nodes(model)
    n_pads = len(pads)

    # Physics requires pads with nets; boards without nets correctly produce
    # an empty proposal.
    if n_pads == 0:
        return PlacementProposal(
            deltas={},
            iterations=0,
            final_max_disp_mm=0.0,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
            forces={},
            stop_reason="empty",
            residual_max_force_N=0.0,
            skipped_uuidless=skipped_uuidless,
            debug_trace={},
        )

    # Group pads by board region
    if model.board_regions and model.footprint_region:
        all_deltas: Dict[str, Tuple[float, float, float]] = {}
        all_label_deltas: Dict[str, Tuple[float, float]] = {}
        all_forces: Dict[str, Tuple[float, float]] = {}
        all_components: Dict[str, Dict[str, Tuple[float, float]]] = {}
        total_iterations = 0
        max_final_disp = 0.0
        all_region_frames: List[List[Dict[str, Tuple[float, float, float]]]] = []
        all_region_force_frames: List[List[Dict[str, Dict[str, Tuple[float, float]]]]] = []
        all_stop = "converged"
        all_residual = 0.0
        all_dbg = {"per_iteration": [], "depen": []}
        any_region = False
        any_depen = False

        for region_idx, region in enumerate(model.board_regions):
            region_pad_indices = []
            region_pads = []
            region_pad_to_fp_idx = []
            region_fp_uuid_to_pad_indices: Dict[str, List[int]] = {}
            region_global_pad_idx_to_local: Dict[int, int] = {}
            region_pad_is_ghost = []

            for i, pad in enumerate(pads):
                fp_idx = pad_to_fp_idx[i]
                if fp_idx >= 0:
                    fp = model.footprints[fp_idx]
                else:
                    ghost_idx = -fp_idx - 1
                    if ghost_idx >= len(model.ghost_footprints):
                        continue
                    fp = model.ghost_footprints[ghost_idx]
                if model.footprint_region.get(fp.uuid) == region_idx:
                    local_idx = len(region_pads)
                    region_pad_indices.append(i)
                    region_pads.append(pad)
                    region_pad_to_fp_idx.append(fp_idx)
                    region_pad_is_ghost.append(pad_is_ghost[i])
                    if fp.uuid not in region_fp_uuid_to_pad_indices:
                        region_fp_uuid_to_pad_indices[fp.uuid] = []
                    region_fp_uuid_to_pad_indices[fp.uuid].append(local_idx)
                    region_global_pad_idx_to_local[local_idx] = global_pad_idx_to_local[i]

            if region_pads:
                (region_deltas, region_forces, region_iters, region_comps,
                 region_frames, region_force_frames, region_stop,
                 region_residual, region_dbg, region_depen,
                 region_label_deltas) = _run_region_simulation(
                    region_idx, model.board_regions[region_idx],
                    region_pad_indices, region_pads, region_pad_to_fp_idx,
                    region_fp_uuid_to_pad_indices, region_global_pad_idx_to_local,
                    region_pad_is_ghost, model, params, movable_uuids,
                    record_frames, _eff_stride, extra_power_nets,
                    freeze_bodies,
                )
                any_region = True
                any_depen = any_depen or region_depen
                if region_stop not in ("converged", "empty"):
                    all_stop = region_stop
                all_residual = max(all_residual, region_residual)
                all_deltas.update(region_deltas)
                all_label_deltas.update(region_label_deltas)
                all_forces.update(region_forces)
                all_components.update(region_comps)
                total_iterations = max(total_iterations, region_iters)
                max_final_disp = max(max_final_disp, max(
                    (math.hypot(dx, dy) for dx, dy, _ in region_deltas.values()),
                    default=0.0,
                ))
                if record_frames and region_frames:
                    all_region_frames.append(region_frames)
                    all_region_force_frames.append(region_force_frames)
                all_dbg["per_iteration"].extend(region_dbg.get("per_iteration", []))
                all_dbg["depen"].extend(region_dbg.get("depen", []))

        merged_frames = _thin_frames(
            _merge_region_lists(all_region_frames), max_frames
        ) if record_frames else []
        merged_force_frames = _thin_frames(
            _merge_region_lists(all_region_force_frames), max_frames
        ) if record_frames else []

        # Post-hoc legality check on final merged deltas
        legality = check_legality(
            model, all_deltas,
            overlap_tolerance_mm2=params.overlap_tolerance_mm2,
            wall_tolerance_mm=params.wall_tolerance_mm,
        )
        final_stop = all_stop if any_region else "empty"
        if final_stop == "converged" and not freeze_bodies and not legality.is_legal:
            final_stop = "stalled"

        return PlacementProposal(
            deltas=all_deltas,
            label_deltas=all_label_deltas,
            iterations=total_iterations,
            final_max_disp_mm=max_final_disp,
            elapsed_s=time.perf_counter() - start,
            params=params.to_dict(),
            forces=all_forces,
            force_components=all_components,
            frames=merged_frames,
            force_frames=merged_force_frames,
            frame_stride=_eff_stride if record_frames else 1,
            stop_reason=final_stop,
            residual_max_force_N=all_residual,
            depenetrated=any_depen,
            skipped_uuidless=skipped_uuidless,
            legality_is_legal=legality.is_legal,
            overlapping_pairs=legality.overlapping_pairs,
            wall_violations=legality.wall_violations,
            max_pair_overlap_mm2=legality.max_pair_overlap_mm2,
            max_wall_penetration_mm=legality.max_wall_penetration_mm,
            exempt_pairs=legality.exempt_pairs,
            degenerate_bodies=tuple(sorted(
                fp.ref for fp in model.footprints
                if fp.ref and courtyard_is_degenerate(fp)
            )),
            debug_trace=all_dbg,
        )

    # Single region mode: create a virtual region covering the whole board
    min_x, max_x, min_y, max_y = _board_bbox(model)
    virtual_region = BoardRegion(
        uuid="virtual",
        polygon=(),
        bbox=(min_x, max_x, min_y, max_y),
    )

    region_pad_indices = list(range(n_pads))
    region_pads = pads
    region_pad_to_fp_idx = pad_to_fp_idx
    region_fp_uuid_to_pad_indices = fp_uuid_to_pad_indices
    region_global_pad_idx_to_local = global_pad_idx_to_local
    region_pad_is_ghost = pad_is_ghost

    (region_deltas, region_forces, region_iters, region_comps,
     region_frames, region_force_frames, region_stop,
     region_residual, region_dbg, region_depen,
     region_label_deltas) = _run_region_simulation(
        0, virtual_region,
        region_pad_indices, region_pads, region_pad_to_fp_idx,
        region_fp_uuid_to_pad_indices, region_global_pad_idx_to_local,
        region_pad_is_ghost, model, params, movable_uuids,
        record_frames, _eff_stride, extra_power_nets,
        freeze_bodies,
    )

    thin_frames = _thin_frames(region_frames, max_frames) if record_frames else []
    thin_force_frames = _thin_frames(region_force_frames, max_frames) if record_frames else []

    # Post-hoc legality check on final deltas
    legality = check_legality(
        model, region_deltas,
        overlap_tolerance_mm2=params.overlap_tolerance_mm2,
        wall_tolerance_mm=params.wall_tolerance_mm,
    )
    final_stop = region_stop
    if final_stop == "converged" and not freeze_bodies and not legality.is_legal:
        final_stop = "stalled"

    return PlacementProposal(
        deltas=region_deltas,
        label_deltas=region_label_deltas,
        iterations=region_iters,
        final_max_disp_mm=max(
            (math.hypot(dx, dy) for dx, dy, _ in region_deltas.values()), default=0.0
        ),
        elapsed_s=time.perf_counter() - start,
        params=params.to_dict(),
        forces=region_forces,
        force_components=region_comps,
        frames=thin_frames,
        force_frames=thin_force_frames,
        frame_stride=_eff_stride if record_frames else 1,
        stop_reason=final_stop,
        residual_max_force_N=region_residual,
        depenetrated=region_depen,
        skipped_uuidless=skipped_uuidless,
        legality_is_legal=legality.is_legal,
        overlapping_pairs=legality.overlapping_pairs,
        wall_violations=legality.wall_violations,
        max_pair_overlap_mm2=legality.max_pair_overlap_mm2,
        max_wall_penetration_mm=legality.max_wall_penetration_mm,
        exempt_pairs=legality.exempt_pairs,
        degenerate_bodies=tuple(sorted(
            fp.ref for fp in model.footprints
            if fp.ref and courtyard_is_degenerate(fp)
        )),
        debug_trace=region_dbg,
    )


def run_label_settle(
    model: BoardModel,
    params: PlacementParams,
    max_iterations: int = 200,
) -> PlacementProposal:
    """Frozen-body label relaxation: settle labels, move no footprints.

    Runs the normal region simulations with bodies frozen, so each label
    orbits (tether + tiny-courtyard wall law + pin discs + board edge +
    pad Coulomb, all one-way) to a local de-overlap equilibrium against
    static obstacles. Returns the usual proposal with empty pad deltas;
    ``stop_reason`` reports label quiet (``converged``/``stalled``).
    """
    from dataclasses import replace

    return run_placement(
        model, replace(params, max_iterations=max_iterations),
        freeze_bodies=True,
    )
