"""Shared label de-penetration: the parts' geometric eject for labels.

Covers the sibling pass in ``placement.depenetrate`` directly (pure
geometry, no placement run): overlapping labels split evenly, a label
takes the full correction against static bodies (one-way invariant),
and clean labels are untouched. Body obstacles go through the same
``_body_poly_map`` the parts pass uses (ghost courtyards here — no pad
rows needed); label bodies are the halo-inflated rects the wall law
separates, same relax, no DRC margins.
"""

import math

import numpy as np
import pytest
from shapely.geometry import Polygon

from kicad_autorouter.board_model import Footprint, Point
from kicad_autorouter.placement.depenetrate import _depenetrate_labels


def _ghost(uuid, x0, y0, x1, y1):
    return Footprint(
        ref="", footprint_id="test:wall", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=(),
        courtyard=(
            (Point(x0, y0), Point(x1, y0)),
            (Point(x1, y0), Point(x1, y1)),
            (Point(x1, y1), Point(x0, y1)),
            (Point(x0, y1), Point(x0, y0)),
        ),
        locked=False, uuid=uuid, ghost=True, ghost_attractions=(),
    )


def _specs(nodes, angle=0.0):
    return [
        (f"u{i}", "R10", (1.0, 1.0), angle, nodes[i], f"parent-{i}")
        for i in range(len(nodes))
    ]


def _bodies(*ghosts):
    uuids = [g.uuid for g in ghosts]
    return (
        uuids,
        [np.array([], dtype=int) for _ in uuids],
        {g.uuid: g for g in ghosts},
    )


@pytest.mark.unit
def test_overlapping_labels_split_evenly():
    pos = np.array([[0.0, 0.0], [0.3, 0.0]], dtype=np.float64)
    nodes = [(0.0, 0.0), (0.3, 0.0)]
    assert _depenetrate_labels(
        pos, [0, 1], _specs(nodes), [], [], {}, [], None, 5.0)
    assert pos[0, 0] < 0.0 < pos[1, 0]
    assert math.isclose(pos[0, 0] + pos[1, 0], 0.3, abs_tol=1e-6)
    from kicad_autorouter.board_model.labels import (
        label_board_rect,
        label_halo_mm,
    )
    halo = label_halo_mm((1.0, 1.0))
    pa = Polygon(label_board_rect(
        "R10", pos[0, 0], pos[0, 1], 0.0, (1.0, 1.0), halo_mm=halo))
    pb = Polygon(label_board_rect(
        "R10", pos[1, 0], pos[1, 1], 0.0, (1.0, 1.0), halo_mm=halo))
    assert pa.disjoint(pb) or pa.intersection(pb).area <= 1e-6


@pytest.mark.unit
def test_label_takes_full_correction_against_body():
    ghost = _ghost("other", -1.0, -1.0, 1.0, 1.0)
    uuids, rows, by_uuid = _bodies(ghost)
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    assert _depenetrate_labels(
        pos, [0], _specs([(0.0, 0.0)]),
        uuids, rows, by_uuid, [], None, 5.0)
    from kicad_autorouter.board_model.labels import (
        label_board_rect,
        label_halo_mm,
    )
    rect = Polygon(label_board_rect(
        "R10", pos[0, 0], pos[0, 1], 0.0, (1.0, 1.0),
        halo_mm=label_halo_mm((1.0, 1.0))))
    body = Polygon([(-1, -1), (1, -1), (1, 1), (-1, 1)])
    assert rect.disjoint(body) or rect.intersection(body).area <= 1e-6


@pytest.mark.unit
def test_own_parent_body_is_sitable():
    # Silk over its own courtyard is DRC-legal, so the parent body never
    # ejects its own label (its copper still would — covered separately).
    ghost = _ghost("parent-0", -1.0, -1.0, 1.0, 1.0)
    uuids, rows, by_uuid = _bodies(ghost)
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    specs = [("u0", "R10", (1.0, 1.0), 0.0, (0.0, 0.0), "parent-0")]
    assert not _depenetrate_labels(
        pos, [0], specs, uuids, rows, by_uuid, [], None, 5.0)
    assert pos[0, 0] == 0.0 and pos[0, 1] == 0.0


@pytest.mark.unit
def test_clean_label_untouched():
    ghost = _ghost("other", 10.0, 10.0, 12.0, 12.0)
    uuids, rows, by_uuid = _bodies(ghost)
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    assert not _depenetrate_labels(
        pos, [0], _specs([(0.0, 0.0)]),
        uuids, rows, by_uuid, [], None, 5.0)
    assert pos[0, 0] == 0.0 and pos[0, 1] == 0.0
