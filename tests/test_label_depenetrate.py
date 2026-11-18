"""Shared label de-penetration: the parts' geometric eject for labels.

Covers the sibling pass in ``placement.depenetrate`` directly (pure
geometry, no placement run): overlapping labels split evenly, a label
takes the full correction against static bodies (one-way invariant),
own-parent silk strokes eject (ink touching text trips silk clearance
even though sitting inside the own courtyard stays legal), and clean
labels are untouched. Body obstacles go through the same
``_body_poly_map`` the parts pass uses (ghost courtyards here — no pad
rows needed); label bodies are the halo-inflated rects the wall law
separates, same relax, no DRC margins.
"""

import math

import numpy as np
import pytest
from shapely.geometry import LineString, Polygon

from kicad_autorouter.board_model import Footprint, Point, SilkSegment
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
    from kicad_autorouter.board_model.labels import label_board_rect
    pa = Polygon(label_board_rect(
        "R10", pos[0, 0], pos[0, 1], 0.0, (1.0, 1.0), halo_mm=0.0))
    pb = Polygon(label_board_rect(
        "R10", pos[1, 0], pos[1, 1], 0.0, (1.0, 1.0), halo_mm=0.0))
    assert pa.disjoint(pb) or pa.intersection(pb).area <= 1e-6


@pytest.mark.unit
def test_label_takes_full_correction_against_body():
    ghost = _ghost("other", -1.0, -1.0, 1.0, 1.0)
    uuids, rows, by_uuid = _bodies(ghost)
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    assert _depenetrate_labels(
        pos, [0], _specs([(0.0, 0.0)]),
        uuids, rows, by_uuid, [], None, 5.0)
    from kicad_autorouter.board_model.labels import label_board_rect
    rect = Polygon(label_board_rect(
        "R10", pos[0, 0], pos[0, 1], 0.0, (1.0, 1.0), halo_mm=0.0))
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


def _silk_ghost(uuid, segments):
    base = _ghost(uuid, 10.0, 10.0, 12.0, 12.0)
    from dataclasses import replace
    return replace(base, silk=tuple(segments))


def _inked_rect(text, x, y, angle=0.0):
    # Bare text rect: the finish scores true ink overlap (the halo is the
    # loop's soft settlement force, never finish dirt).
    from kicad_autorouter.board_model.labels import label_board_rect
    return Polygon(label_board_rect(
        text, x, y, angle, (1.0, 1.0), halo_mm=0.0))


@pytest.mark.unit
def test_own_parent_silk_ejects_label():
    # A label parked on its own parent's silk stroke is pushed clear:
    # ink touching text trips silk clearance even though the own
    # courtyard stays sit-able ground.
    parent = _silk_ghost("parent-0", [
        SilkSegment(Point(-3.0, 0.0), Point(3.0, 0.0), 0.12),
    ])
    uuids, rows, by_uuid = _bodies(parent)
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    specs = [("u0", "R10", (1.0, 1.0), 0.0, (0.0, 0.0), "parent-0")]
    assert _depenetrate_labels(
        pos, [0], specs, uuids, rows, by_uuid, [], None, 5.0)
    assert math.hypot(pos[0, 0], pos[0, 1]) <= 5.0
    rect = _inked_rect("R10", pos[0, 0], pos[0, 1])
    ink = LineString([(-3.0, 0.0), (3.0, 0.0)]).buffer(0.06 + 0.2)
    assert rect.disjoint(ink) or rect.intersection(ink).area <= 1e-6


@pytest.mark.unit
def test_sweep_scores_label_rect_not_nearby_silk():
    # Regression: the sweep's nearby-silk collection loop once rebound
    # the ``poly`` variable holding the label rect to the last silk
    # stroke, so a clean label with silk within anchor reach was scored
    # — and relocated — as the silk rect instead of its own rect.
    parent = _ghost("parent-0", 10.0, 10.0, 12.0, 12.0)
    blocker = _ghost("blocker", 4.0, -0.5, 5.0, 0.5)
    from dataclasses import replace
    silked = replace(
        _ghost("silk-parent", 10.0, 10.0, 12.0, 12.0),
        silk=(SilkSegment(Point(3.5, 0.0), Point(5.5, 0.0), 0.12),),
    )
    uuids = [parent.uuid, blocker.uuid, silked.uuid]
    rows = [np.array([], dtype=int) for _ in uuids]
    by_uuid = {g.uuid: g for g in (parent, blocker, silked)}
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    specs = [("u0", "R10", (1.0, 1.0), 0.0, (0.0, 0.0), "parent-0")]
    assert not _depenetrate_labels(
        pos, [0], specs, uuids, rows, by_uuid, [], None, 5.0)
    assert pos[0, 0] == 0.0 and pos[0, 1] == 0.0


@pytest.mark.unit
def test_own_pad_copper_ejects_label():
    # Own copper is a real obstacle: a label parked on its parent's pads
    # must move off, not sit on copper. The eject pushes it out and the
    # search relocates it to nearest-clean.
    parent = _ghost("parent-0", 10.0, 10.0, 12.0, 12.0)
    uuids, rows, by_uuid = _bodies(parent)
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    specs = [("u0", "R10", (1.0, 1.0), 0.0, (0.0, 0.0), "parent-0")]
    copper = [Polygon([(0.5, -0.5), (1.5, -0.5), (1.5, 0.5), (0.5, 0.5)])]
    assert _depenetrate_labels(
        pos, [0], specs, uuids, rows, by_uuid, copper, None, 5.0)
    rect = _inked_rect("R10", pos[0, 0], pos[0, 1])
    assert rect.disjoint(copper[0]) \
        or rect.intersection(copper[0]).area <= 1e-6


def _square_loop(half):
    pts = [(-half, -half), (half, -half), (half, half), (-half, half)]
    return [
        SilkSegment(Point(*pts[i]), Point(*pts[(i + 1) % 4]), 0.12)
        for i in range(4)
    ]


@pytest.mark.unit
def test_clean_spot_finds_gap_and_reports_trapped():
    from shapely.geometry import box

    from kicad_autorouter.placement.depenetrate import _clean_spot
    rect = box(-1.0, -1.0, 1.0, 1.0)
    # Walls overlap three sides, open to the east: the first clean spiral
    # hit must be straight +x (dust-tolerant touching does not count).
    walls = [
        box(-3.0, -4.0, -0.5, 4.0),
        box(-4.0, 0.5, 4.0, 4.0),
        box(-4.0, -4.0, 4.0, -0.5),
    ]
    spot = _clean_spot(rect, walls, (0.0, 0.0), 5.0)
    assert spot is not None and spot[0] > 1.0 and abs(spot[1]) < 1e-9
    # Sealed on all sides past reach: no candidate is clean.
    cage = walls + [
        box(0.5, -4.0, 7.0, 4.0),
        box(-7.0, 0.5, 7.0, 6.0),
        box(-7.0, -6.0, 7.0, -0.5),
        box(-7.0, -4.0, -0.5, 4.0),
    ]
    assert _clean_spot(rect, cage, (0.0, 0.0), 5.0) is None
    # Clean where it sits never moves, even off-center.
    assert _clean_spot(rect, [], (3.0, 4.0), 5.0) == (0.0, 0.0)


@pytest.mark.unit
def test_best_spot_minimizes_when_nothing_is_clean():
    from shapely.geometry import box

    from kicad_autorouter.placement.depenetrate import (
        _best_spot,
        _score_spots,
    )
    rect = box(-1.0, -1.0, 1.0, 1.0)
    cage = [
        box(-3.0, -4.0, -0.5, 4.0),
        box(-4.0, 0.5, 4.0, 4.0),
        box(-4.0, -4.0, 4.0, -0.5),
        box(0.5, -4.0, 7.0, 4.0),
        box(-7.0, 0.5, 7.0, 6.0),
        box(-7.0, -6.0, 7.0, -0.5),
        box(-7.0, -4.0, -0.5, 4.0),
    ]
    scored = list(_score_spots(rect, cage, (0.0, 0.0), 5.0))
    center_area = scored[0][1]
    best, area = _best_spot(rect, cage, (0.0, 0.0), 5.0)
    assert best is not None
    assert area == min(a for _, a in scored)
    assert area < center_area - 1e-9


@pytest.mark.unit
def test_trapped_in_closed_loop_searches_clean_spot():
    # D1-style trap: a label centered in an enclosing outline gets equal
    # "away from nearest ink" pushes from all sides that cancel. The
    # clean-spot search resolves it by testing candidates instead of
    # pushing: first fully-clean spiral spot from the anchor wins. The
    # loop is sized so bare text touches the enclosing strokes (halo is
    # loop-only, never finish dirt).
    parent = _silk_ghost("parent-0", _square_loop(0.7))
    uuids, rows, by_uuid = _bodies(parent)
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    specs = [("u0", "D1", (1.0, 1.0), 0.0, (2.0, 0.0), "parent-0")]
    assert _depenetrate_labels(
        pos, [0], specs, uuids, rows, by_uuid, [], None, 6.0)
    assert math.hypot(pos[0, 0] - 2.0, pos[0, 1]) <= 6.0
    rect = _inked_rect("D1", pos[0, 0], pos[0, 1])
    for s in parent.silk:
        ink = LineString([
            (s.start.x_mm, s.start.y_mm), (s.end.x_mm, s.end.y_mm),
        ]).buffer(s.width_mm / 2.0 + 0.2)
        assert rect.disjoint(ink) or rect.intersection(ink).area <= 1e-6


@pytest.mark.unit
def test_search_reaches_neighbor_silk():
    # D5/J2 case: the label overlaps a neighbor parent's strokes, which
    # ejectors ignore by design. The search tests the full obstacle set
    # (own + nearby-neighbor silk), so it relocates fully clean.
    parent = _silk_ghost("parent-0", [])
    other = _silk_ghost("other", [
        SilkSegment(Point(-3.0, 0.0), Point(3.0, 0.0), 0.12),
    ])
    uuids, rows, by_uuid = _bodies(parent, other)
    pos = np.array([[0.0, 0.0]], dtype=np.float64)
    specs = [("u0", "R10", (1.0, 1.0), 0.0, (0.0, 0.0), "parent-0")]
    assert _depenetrate_labels(
        pos, [0], specs, uuids, rows, by_uuid, [], None, 5.0)
    rect = _inked_rect("R10", pos[0, 0], pos[0, 1])
    for s in other.silk:
        ink = LineString([
            (s.start.x_mm, s.start.y_mm), (s.end.x_mm, s.end.y_mm),
        ]).buffer(s.width_mm / 2.0 + 0.2)
        assert rect.disjoint(ink) or rect.intersection(ink).area <= 1e-6
