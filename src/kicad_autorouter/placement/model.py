"""Pad/node collection and net-edge construction for placement dynamics.

Collects pads with net connections from real and ghost footprints, builds
decoupling-cap detection, and constructs pad-level attraction edge arrays.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from ..board_model import BoardModel, Footprint, Pad, is_ground_net, is_power_net_effective


def _collect_pad_nodes(
    model: BoardModel,
) -> Tuple[List[Pad], List[int], Dict[str, List[int]], Dict[int, int], List[bool]]:
    """Collect all pads with net connections from both real and ghost footprints.

    Returns:
        - pads: list of Pad objects with net_name
        - pad_to_fp_idx: list mapping pad index -> footprint index (negative for ghost footprints)
        - fp_uuid_to_pad_indices: dict mapping footprint UUID -> list of global pad indices
        - global_pad_idx_to_local: dict mapping global pad index -> local pad index within footprint
        - pad_is_ghost: list of bool indicating if pad belongs to a ghost footprint
    """
    pads = []
    pad_to_fp_idx = []
    fp_uuid_to_pad_indices = {}
    global_pad_idx_to_local = {}
    pad_is_ghost = []

    # Real footprints (positive indices)
    for fp_idx, fp in enumerate(model.footprints):
        if not fp.uuid:
            continue
        fp_pad_indices = []
        for local_pad_idx, pad in enumerate(fp.pads):
            if pad.net_name is None:
                continue
            global_pad_idx = len(pads)
            fp_pad_indices.append(global_pad_idx)
            global_pad_idx_to_local[global_pad_idx] = local_pad_idx
            pads.append(pad)
            pad_to_fp_idx.append(fp_idx)
            pad_is_ghost.append(False)
        if fp_pad_indices:
            fp_uuid_to_pad_indices[fp.uuid] = fp_pad_indices

    # Ghost footprints (negative indices: -(ghost_idx + 1))
    for ghost_idx, fp in enumerate(model.ghost_footprints):
        if not fp.uuid:
            continue
        fp_pad_indices = []
        for local_pad_idx, pad in enumerate(fp.pads):
            if pad.net_name is None:
                continue
            global_pad_idx = len(pads)
            fp_pad_indices.append(global_pad_idx)
            global_pad_idx_to_local[global_pad_idx] = local_pad_idx
            pads.append(pad)
            pad_to_fp_idx.append(-(ghost_idx + 1))  # negative = ghost
            pad_is_ghost.append(True)
        if fp_pad_indices:
            fp_uuid_to_pad_indices[fp.uuid] = fp_pad_indices

    return pads, pad_to_fp_idx, fp_uuid_to_pad_indices, global_pad_idx_to_local, pad_is_ghost


def _find_decap_pad_indices(
    pads: List[Pad],
    pad_to_fp_idx: List[int],
    fp_ref_by_idx: Dict[int, str],
    extra_power: Collection[str] = (),
) -> Set[int]:
    """Pad indices belonging to decoupling caps in this pad set.

    A decap is a footprint whose ref starts with "C", contributing exactly
    2 netted pads here, with one side on a power rail and the other on
    ground (effective-power membership, so user-marked rails count).
    Ghost footprints are never decaps. Used to (a) boost decap-incident edges
    and (b) admit the decap's GND-side edges that are otherwise excluded
    (GND pours connect anywhere, so general GND attraction would only
    clump parts).
    """
    fp_to_rows: Dict[int, List[int]] = {}
    for i, fp_idx in enumerate(pad_to_fp_idx):
        if fp_idx < 0:
            continue  # ghost footprint
        fp_to_rows.setdefault(fp_idx, []).append(i)
    decap: Set[int] = set()
    for fp_idx, rows in fp_to_rows.items():
        if len(rows) != 2:
            continue
        ref = fp_ref_by_idx.get(fp_idx, "")
        if not ref.upper().startswith("C"):
            continue
        nets = [pads[i].net_name for i in rows]
        sides = sorted(
            "power" if is_power_net_effective(n, extra_power) else ("ground" if is_ground_net(n) else "signal")
            for n in nets
        )
        if sides == ["ground", "power"]:
            decap.update(rows)
    return decap


def _build_net_edges_pad_level(
    pads: List[Pad],
    pad_to_fp_idx: List[int],
    decap_pad_indices: Optional[Set[int]] = None,
    power_scale: float = 1.0,
    decap_boost: float = 4.0,
    include_power: bool = True,
    extra_power: Collection[str] = (),
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build edge index arrays for pad pairs sharing the same net.

    Returns (src_indices, dst_indices, weights) where each pair appears
    once (i < j). Only connects pads on different footprints. Weights are
    per-edge ka multipliers applied by the attraction kernel:

    - signal nets: 1.0 (unchanged behavior);
    - power nets (keyword match or user-marked ``extra_power``):
      power_scale / (fanout - 1) — a rail shared by many
      parts pulls each pad about as hard in total as one signal edge,
      so global rails minimize wirelength without collapsing the board;
    - ground nets: excluded (0 edges) unless at least one endpoint is a
      decap pad (see _find_decap_pad_indices), in which case the edge is
      admitted at the decap-boosted power rate. Pours connect GND
      anywhere, so general GND attraction has no placement value.
    - any edge incident to a decap pad additionally gets x decap_boost.

    With include_power=False, power edges are dropped (decap power-side
    edges go with them); the decap GND-side exception is gated by
    decap_boost > 0 instead.
    """
    decap_set = decap_pad_indices or set()

    net_to_pads: Dict[str, List[int]] = {}
    for i, pad in enumerate(pads):
        if pad.net_name:
            net_to_pads.setdefault(pad.net_name, []).append(i)

    src_list: List[int] = []
    dst_list: List[int] = []
    weight_list: List[float] = []
    for net_name, pad_indices in net_to_pads.items():
        fanout = len(pad_indices)
        if fanout < 2:
            continue
        net_is_power = is_power_net_effective(net_name, extra_power)
        net_is_ground = is_ground_net(net_name)
        if net_is_power and not include_power:
            continue
        if net_is_power:
            base = power_scale / (fanout - 1)
        elif net_is_ground:
            base = power_scale / (fanout - 1)
        else:
            base = 1.0
        if base == 0.0:
            continue
        for a in range(len(pad_indices)):
            for b in range(a + 1, len(pad_indices)):
                i, j = pad_indices[a], pad_indices[b]
                # Only connect pads on different footprints
                if pad_to_fp_idx[i] == pad_to_fp_idx[j]:
                    continue
                if net_is_ground and i not in decap_set and j not in decap_set:
                    continue  # GND pour: no general attraction
                w = base
                if i in decap_set or j in decap_set:
                    w *= decap_boost
                if w == 0.0:
                    continue
                src_list.append(i)
                dst_list.append(j)
                weight_list.append(w)

    if not src_list:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([], dtype=float)

    order = sorted(range(len(src_list)), key=lambda k: (src_list[k], dst_list[k]))
    return (
        np.array([src_list[k] for k in order], dtype=int),
        np.array([dst_list[k] for k in order], dtype=int),
        np.array([weight_list[k] for k in order], dtype=float),
    )
