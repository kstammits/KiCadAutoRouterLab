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
    """Collect all pads (netted and unconnected) from real and ghost footprints.

    Unconnected copper (mounting pins on netted parts) joins as a node
    too — for geometry only: it anchors the rigid fit and the courtyard
    re-fit, but carries no Coulomb charge (see ForceContext.pad_charged)
    and no attraction edges, so it collides by contact without dragging
    or repelling neighbors at range. Excluding it degrades the Kabsch fit for
    parts whose netted pads are clustered or collinear — e.g. pots whose
    wide-stance mounting pins are the only well-conditioned anchors — and
    desyncs the courtyard re-fit from the footprint pads. Footprints with
    only unconnected pads (mounting holes) stay exactly as before: with
    no electrical participation they hold their filed positions instead
    of gaining repulsion from nothing.

    Returns:
        - pads: list of Pad objects (net_name None for unconnected copper)
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
        if not any(pad.net_name for pad in fp.pads):
            # No electrical participation (mounting holes): hold position
            # exactly as before instead of gaining repulsion from nothing.
            continue
        fp_pad_indices = []
        for local_pad_idx, pad in enumerate(fp.pads):
            global_pad_idx = len(pads)
            fp_pad_indices.append(global_pad_idx)
            global_pad_idx_to_local[global_pad_idx] = local_pad_idx
            pads.append(pad)
            pad_to_fp_idx.append(fp_idx)
            pad_is_ghost.append(False)
        if fp_pad_indices:
            fp_uuid_to_pad_indices[fp.uuid] = fp_pad_indices

    # Ghost footprints (negative indices: -(ghost_idx + 1)). Ghost pads
    # are anchors, not copper: keep the historical netted-only membership
    # so attractor/rail geometry never gains Coulomb repulsion.
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
        # Count netted pads only: an unconnected shield/mount pin must not
        # disqualify an otherwise exact 2-pin power+ground decap.
        netted = [i for i in rows if pads[i].net_name]
        if len(netted) != 2:
            continue
        ref = fp_ref_by_idx.get(fp_idx, "")
        if not ref.upper().startswith("C"):
            continue
        nets = [pads[i].net_name for i in netted]
        sides = sorted(
            "power" if is_power_net_effective(n, extra_power) else ("ground" if is_ground_net(n) else "signal")
            for n in nets
        )
        if sides == ["ground", "power"]:
            decap.update(netted)
    return decap


def _build_net_edges_pad_level(
    pads: List[Pad],
    pad_to_fp_idx: List[int],
    decap_pad_indices: Optional[Set[int]] = None,
    power_scale: float = 1.0,
    decap_link: float = 4.0,
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
      admitted at the same fanout-scaled power rate. Pours connect GND
      anywhere, so general GND attraction has no placement value.
    - ``decap_link`` scales only the supplemental symmetric local
      decap-chip links (see _find_decap_chip_links), never the global
      rail clique above, so the chip link overpowers rail fanout.

    With include_power=False, power edges are dropped (decap power-side
    edges go with them); the decap GND-side exception is gated by
    decap_link > 0 instead (0 also disables the local chip links).
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
                if net_is_ground and decap_link == 0.0:
                    continue  # kill-switch: no decap GND edges, no locals
                w = base
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


# Supplemental decap-chip links: small caps hug their nearest chip.
#
# The global rail clique above is deliberately kept: it carries the
# rail-balance pull and rotates chips. These local edges are additive:
# one symmetric (both sides feel it) edge per shared net between a
# small decap pad and the nearest same-net pad of its linked chip.
# Caps with no close eligible chip stay unassigned (global-only) rather
# than latching onto a distant or ambiguous neighbor; bulk electrolytics
# (large courtyard) never link and keep global-only behavior near the
# board inlet.
_DECAP_LOCAL_MAX_AREA_M2 = 15e-6
_DECAP_LINK_MAX_DIST_MM = 15.0


def _find_decap_chip_links(
    pads: List[Pad],
    pad_to_fp_idx: List[int],
    fp_ref_by_idx: Dict[int, str],
    fp_area_m2_by_idx: Optional[Dict[int, float]] = None,
    max_dist_mm: float = _DECAP_LINK_MAX_DIST_MM,
    extra_power: Collection[str] = (),
) -> Tuple[List[Tuple[int, int]], Dict[str, Optional[str]]]:
    """Link each small decap footprint to its nearest chip footprint.

    Eligibility: ``C*`` + exactly 2 netted pads + power/ground (same rule
    as :func:`_find_decap_pad_indices`), courtyard area below
    ``_DECAP_LOCAL_MAX_AREA_M2`` (unknown area counts as small so
    synthetic tests exercise links), and a ``U*``-ref footprint sharing
    at least one net within ``max_dist_mm`` (filed-pad centroid
    distance, nearest wins, no runner-up margin).

    Returns ``(edges, links)`` where edges are ``(pad_i, pad_j)`` pairs
    (one per shared-net pad of the decap, nearest same-net chip pad)
    and links maps decap ref -> chip ref (or None when unassigned, with
    bulk decaps always None).
    """
    areas = fp_area_m2_by_idx or {}
    fp_to_rows: Dict[int, List[int]] = {}
    for i, fp_idx in enumerate(pad_to_fp_idx):
        if fp_idx < 0:
            continue
        fp_to_rows.setdefault(fp_idx, []).append(i)

    def _centroid(rows: List[int]) -> Optional[Tuple[float, float]]:
        xs, ys = [], []
        for i in rows:
            try:
                xs.append(float(pads[i].position.x_mm))
                ys.append(float(pads[i].position.y_mm))
            except (AttributeError, TypeError, ValueError):
                return None
        if not xs:
            return None
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    # Decap footprints under the shared rule, in deterministic ref order.
    decap_fps: List[int] = []
    for fp_idx, rows in fp_to_rows.items():
        netted = [i for i in rows if pads[i].net_name]
        if len(netted) != 2:
            continue
        ref = fp_ref_by_idx.get(fp_idx, "")
        if not ref.upper().startswith("C"):
            continue
        nets = [pads[i].net_name for i in netted]
        sides = sorted(
            "power" if is_power_net_effective(n, extra_power) else ("ground" if is_ground_net(n) else "signal")
            for n in nets
        )
        if sides == ["ground", "power"]:
            decap_fps.append(fp_idx)
    decap_fps.sort(key=lambda f: fp_ref_by_idx.get(f, ""))

    chip_fps = [
        fp_idx for fp_idx, rows in fp_to_rows.items()
        if (fp_ref_by_idx.get(fp_idx, "").upper().startswith("U"))
    ]

    fp_nets: Dict[int, Set[str]] = {}
    fp_cent: Dict[int, Tuple[float, float]] = {}
    for fp_idx, rows in fp_to_rows.items():
        fp_nets[fp_idx] = {pads[i].net_name for i in rows if pads[i].net_name}
        c = _centroid(rows)
        if c is not None:
            fp_cent[fp_idx] = c

    edges: List[Tuple[int, int]] = []
    links: Dict[str, Optional[str]] = {}
    for fp_idx in decap_fps:
        ref = fp_ref_by_idx.get(fp_idx, "")
        area = areas.get(fp_idx)
        if area is not None and area >= _DECAP_LOCAL_MAX_AREA_M2:
            links[ref] = None  # bulk electrolytic: global-only
            continue
        me = fp_cent.get(fp_idx)
        shared = fp_nets.get(fp_idx, set())
        best: Optional[int] = None
        best_d = float("inf")
        if me is not None:
            for cand in chip_fps:
                if cand == fp_idx or cand not in fp_cent:
                    continue
                if not (shared & fp_nets.get(cand, set())):
                    continue
                cx, cy = fp_cent[cand]
                d = ((me[0] - cx) ** 2 + (me[1] - cy) ** 2) ** 0.5
                if d < best_d:
                    best_d, best = d, cand
        if best is None or best_d > max_dist_mm:
            links[ref] = None  # distant/ambiguous: leave unassigned
            continue
        links[ref] = fp_ref_by_idx.get(best, "")
        chip_rows = fp_to_rows.get(best, [])
        chip_by_net: Dict[str, List[int]] = {}
        for j in chip_rows:
            if pads[j].net_name:
                chip_by_net.setdefault(pads[j].net_name, []).append(j)
        for i in [r for r in fp_to_rows[fp_idx] if pads[r].net_name]:
            cands = chip_by_net.get(pads[i].net_name, [])
            if not cands:
                continue
            try:
                ix, iy = float(pads[i].position.x_mm), float(pads[i].position.y_mm)
            except (AttributeError, TypeError, ValueError):
                continue
            jbest, jd = cands[0], float("inf")
            for j in cands:
                try:
                    jx, jy = float(pads[j].position.x_mm), float(pads[j].position.y_mm)
                except (AttributeError, TypeError, ValueError):
                    continue
                d = ((ix - jx) ** 2 + (iy - jy) ** 2) ** 0.5
                if d < jd:
                    jd, jbest = d, j
            edges.append((i, jbest))
    return edges, links


# Public aliases (non-private) for consumers outside the package, e.g. the UI
# server. Behavior is identical to the underscore originals.
collect_pad_nodes = _collect_pad_nodes
find_decap_chip_links = _find_decap_chip_links
