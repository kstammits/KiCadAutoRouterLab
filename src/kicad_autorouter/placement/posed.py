"""Posed part geometry for the frozen finish phase.

The loop re-poses incrementally every iteration
(:mod:`placement.force` ``poly_cache``); the finish runs on frozen
positions, so one build serves every consumer in the call: the parts
eject, the label eject + search sweeps, and the hygiene seed. Bodies,
copper, and posed silk strokes are built once here instead of once per
consumer; only label rects (nodes move during the search) are rebuilt
per sweep. Exact keying — no tolerance: the finish scores true overlap.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Dict, List

import numpy as np

from ..board_model import Footprint, Point
from .contact import (
    _footprint_courtyard_polygon,
    _pose_ring,
    pad_copper_polygon,
    pad_present_on_layer,
)

logger = logging.getLogger(__name__)


def _body_poly_map(
    positions: np.ndarray,
    fp_uuids: List[str],
    fp_local_to_rows: List[np.ndarray],
    fp_by_uuid: Dict[str, Footprint],
) -> Dict[str, object]:
    """Posed courtyard polygon per footprint uuid (ghosts filed, rest posed).

    Shared body set for both depenetrate passes: ghosts use their filed
    courtyards (static anchors), real footprints rebuild from current pad
    positions. Attractor ghosts (pull-only) are not bodies — only wall
    ghosts join the obstacle set. Skips degenerates, never raises.
    """
    polys: Dict[str, object] = {}
    for li, uuid in enumerate(fp_uuids):
        fp = fp_by_uuid.get(uuid)
        if fp is None:
            continue
        if getattr(fp, "ghost", False) and getattr(fp, "ghost_attractions", ()):
            continue
        try:
            rows = fp_local_to_rows[li]
        except (IndexError, TypeError):
            continue
        if fp.ghost or len(rows) == 0:
            if not fp.courtyard:
                continue
            poly = _footprint_courtyard_polygon(fp)
        else:
            try:
                poly = _footprint_courtyard_polygon(
                    fp, pad_positions=positions, pad_indices=[int(r) for r in rows]
                )
            except Exception:
                logger.debug("courtyard polygon failed for %s", uuid)
                continue
        if poly is None or poly.is_empty:
            continue
        polys[uuid] = poly
    return polys


def _pose_parent_silk(fp, positions, rows):
    """Posed silk strokes + centroid for one footprint.

    Returns ``([(x1, y1, x2, y2, width_mm), ...], (hx, hy))`` in board
    frame. Ghosts (and pad-less footprints) use filed geometry; the rest
    rigid-fit from the pad cloud exactly like courtyard posing. Never
    raises: malformed input yields empty strokes and the filed centroid.
    """
    try:
        home = (float(fp.x_mm), float(fp.y_mm))
    except (TypeError, ValueError, AttributeError):
        return [], (0.0, 0.0)
    segs = getattr(fp, "silk", ()) or ()
    if not segs:
        return [], home
    try:
        if getattr(fp, "ghost", False) or rows is None or len(rows) == 0:
            return [
                (s.start.x_mm, s.start.y_mm, s.end.x_mm, s.end.y_mm,
                 float(s.width_mm))
                for s in segs
            ], home
        pad_rows = [int(r) for r in rows]
        ends = []
        for s in segs:
            ends.append((s.start.x_mm, s.start.y_mm))
            ends.append((s.end.x_mm, s.end.y_mm))
        fitted = _pose_ring(ends, fp, positions, pad_rows)
        posed = [
            (fitted[i][0], fitted[i][1], fitted[i + 1][0], fitted[i + 1][1])
            for i in range(0, len(fitted) - 1, 2)
        ]
        widths = [float(s.width_mm) for s in segs]
        home = _pose_ring([(home[0], home[1])], fp, positions, pad_rows)[0]
        out = []
        for i, (x1, y1, x2, y2) in enumerate(posed):
            try:
                out.append((x1, y1, x2, y2, widths[i]))
            except (IndexError, TypeError, ValueError):
                continue
        return out, (float(home[0]), float(home[1]))
    except Exception:
        return [], home


class PosedGeometry:
    """Frozen-phase part geometry: bodies, copper, and posed silk.

    Built once per finish call and shared by the parts eject, the label
    eject + search sweeps, and the hygiene seed. ``silk_memo`` buffers
    silk stroke polys on first touch for every later query in the call.
    """

    def __init__(self):
        self.bodies: Dict[str, object] = {}
        self.layers: Dict[str, str] = {}
        self.copper: List[object] = []
        self.silk: Dict[str, list] = {}
        self.silk_memo: Dict[tuple, object] = {}


def build_finish_geometry(
    model,
    positions: np.ndarray,
    fp_uuids: List[str],
    fp_local_to_rows: List[np.ndarray],
    fp_by_uuid: Dict[str, Footprint],
    region_pad_to_fp_idx,
    region_pads,
    new_poses,
) -> PosedGeometry:
    """Pose every body, copper shape, and silk stroke once for the finish.

    Same visibility the orbit walls feel: any pad with copper on either
    side (THT spans sides, so it is covered once), all bodies regardless
    of layer. ``new_poses`` supplies the final rigid deltas; ``positions``
    the frozen pad coordinates.
    """
    geo = PosedGeometry()
    geo.bodies = _body_poly_map(positions, fp_uuids, fp_local_to_rows, fp_by_uuid)
    for uuid, poly in geo.bodies.items():
        fp = fp_by_uuid.get(uuid)
        if fp is None:
            continue
        geo.layers[uuid] = fp.layer
    cu_layers = tuple(getattr(model, "copper_layers", ()) or ())
    n_pads = len(region_pads)
    for _i in range(n_pads):
        try:
            _li = region_pad_to_fp_idx[_i]
            if _li < 0:
                continue
            _fp = model.footprints[_li]
            if not _fp.uuid:
                continue
            _pose = new_poses.get(_fp.uuid)
            _da = (_pose[2] - _fp.angle_deg) if _pose is not None else 0.0
            _pad = region_pads[_i]
            _posed = replace(
                _pad,
                position=Point(float(positions[_i, 0]), float(positions[_i, 1])),
                angle_deg=_pad.angle_deg + _da,
            )
            if (pad_present_on_layer(_posed, _fp.layer, "F.Cu", cu_layers)
                    or pad_present_on_layer(_posed, _fp.layer, "B.Cu", cu_layers)):
                _poly = pad_copper_polygon(_posed)
                if _poly is not None and not _poly.is_empty:
                    geo.copper.append(_poly)
        except Exception:
            continue
    _uuid_to_li = {uuid: li for li, uuid in enumerate(fp_uuids)}
    for uuid in fp_uuids:
        fp = fp_by_uuid.get(uuid)
        if fp is None or not fp.silk:
            geo.silk[uuid] = []
            continue
        try:
            li = _uuid_to_li.get(uuid)
            rows = fp_local_to_rows[li] if li is not None else None
            strokes, _home = _pose_parent_silk(fp, positions, rows)
            geo.silk[uuid] = strokes
        except Exception:
            geo.silk[uuid] = []
    return geo
