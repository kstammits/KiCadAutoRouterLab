"""Emit-gate tests: fitted/smoothed copper must respect hard exclusion.

Covers the tube111 pin-run shorts, where the fitter's stitching loop
emitted straight connector stubs across foreign copper the search and
smoother both refused (0.5 mm power tracks through foreign SMD pads).
Unlike the search/smoother gates, the emit gate uses exact
segment-vs-cell geometry (no sampling gaps, no endpoint allowances).
"""

import numpy as np
import pytest

from types import SimpleNamespace

from kicad_autorouter.board_model.model import Footprint, Pad, Point, Via
from kicad_autorouter.routing.verify import (
    fitted_pieces_violate,
    gate_touch_margin,
)


class _Grid:
    def __init__(self, origin=(0.0, 0.0), res=0.1):
        self.origin_mm = origin
        self.resolution_mm = res


def _masks(blocked_cells, shape=(2, 30, 30)):
    blocked = np.zeros(shape, dtype=bool)
    for l, r, c in blocked_cells:
        blocked[l, r, c] = True
    return blocked


def test_clean_chords_pass():
    grid = _Grid()
    blocked = _masks([])
    assert fitted_pieces_violate(
        branches=[[(1, 1, 0), (10, 1, 0)]],
        arc_pieces=[()],
        blocked_mask=blocked, grid=grid) == []


def test_fitted_seg_through_exclusion_fails():
    # Straight 1-cell-wide stub through a blocked cell (the pin-run shape).
    grid = _Grid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])
    problems = fitted_pieces_violate(
        branches=[[(0, 5, 0), (12, 5, 0)]],
        arc_pieces=[[("seg", 0.0, 5.0, 12.0, 5.0, 0)]],
        blocked_mask=blocked, grid=grid)
    assert len(problems) == 2
    assert all("hard exclusion" in p for p in problems)


def test_fitted_arc_bowing_into_exclusion_fails():
    # Circular arc through (2,2)-(10,10)-(18,2) apexes at board (10,10):
    # block that cell (row 10, col 10).
    grid = _Grid()
    blocked = _masks([(0, 10, 10)])
    problems = fitted_pieces_violate(
        branches=[[(2, 2, 0), (18, 2, 0)]],
        arc_pieces=[[("arc", 2.0, 2.0, 10.0, 10.0, 18.0, 2.0, 0)]],
        blocked_mask=blocked, grid=grid)
    assert any("arc" in p for p in problems)


def test_parallel_run_outside_exclusion_passes():
    # A chord one full cell clear of exclusion is legal (toll road, not a
    # short) and must not trip the gate.
    grid = _Grid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])
    assert fitted_pieces_violate(
        branches=[[(0, 7, 0), (12, 7, 0)]],
        arc_pieces=[[("seg", 0.0, 7.0, 12.0, 7.0, 0)]],
        blocked_mask=blocked, grid=grid) == []


class _BoardGrid(_Grid):
    layers = ("F.Cu", "B.Cu")

    @property
    def layer_to_idx(self):
        return {"F.Cu": 0, "B.Cu": 1}


def _smd_model(x_mm, y_mm, net_name, layer="F.Cu", shape="rect",
               size_mm=(1.0, 1.0), ref="U1"):
    fp = Footprint(
        ref=ref, footprint_id="test:fp", layer=layer,
        x_mm=x_mm, y_mm=y_mm, angle_deg=0.0,
        pads=(Pad(number="1", net_name=net_name,
                  position=Point(x_mm, y_mm), size_mm=size_mm,
                  shape=shape, angle_deg=0.0, pad_type="smd",
                  drill_mm=0.0, layers=(layer,)),),
    )
    return SimpleNamespace(footprints=(fp,), vias=(),
                           copper_layers=("F.Cu", "B.Cu"))


def test_gate_touch_reports_real_short():
    # Track centerline over foreign pad copper: real touch, margin 0.
    grid = _BoardGrid()
    model = _smd_model(0.65, 0.55, "X2")
    is_touch, margin = gate_touch_margin(
        [[(0, 5, 0), (12, 5, 0)]], [()], grid, model, "X1", 0.125)
    assert is_touch
    assert margin == 0.0


def test_gate_graze_reports_non_touch():
    # Foreign copper well clear of the track edge: no touch, margin > 0.
    grid = _BoardGrid()
    model = _smd_model(0.65, 2.0, "X2")
    is_touch, margin = gate_touch_margin(
        [[(0, 5, 0), (12, 5, 0)]], [()], grid, model, "X1", 0.125)
    assert not is_touch
    assert margin == pytest.approx(0.825, abs=1e-6)


def test_gate_ignores_own_net_copper():
    # Terminals sit on own copper by construction — never a touch.
    grid = _BoardGrid()
    model = _smd_model(0.65, 0.55, "X1")
    is_touch, margin = gate_touch_margin(
        [[(0, 5, 0), (12, 5, 0)]], [()], grid, model, "X1", 0.125)
    assert not is_touch
    assert margin == float("inf")


def test_gate_via_annulus_touch():
    # Foreign via annulus under the track is a real touch; own-net vias
    # are excluded like own pads.
    grid = _BoardGrid()
    foreign = SimpleNamespace(
        footprints=(), copper_layers=("F.Cu", "B.Cu"),
        vias=(Via(position=Point(0.65, 0.55), size_mm=0.8, drill_mm=0.4,
                  layers=("F.Cu", "B.Cu"), net_name="X2"),),
    )
    is_touch, _ = gate_touch_margin(
        [[(0, 5, 0), (12, 5, 0)]], [()], grid, foreign, "X1", 0.125)
    assert is_touch
    own = SimpleNamespace(
        footprints=(), copper_layers=("F.Cu", "B.Cu"),
        vias=(Via(position=Point(0.65, 0.55), size_mm=0.8, drill_mm=0.4,
                  layers=("F.Cu", "B.Cu"), net_name="X1"),),
    )
    is_touch, margin = gate_touch_margin(
        [[(0, 5, 0), (12, 5, 0)]], [()], grid, own, "X1", 0.125)
    assert not is_touch
    assert margin == float("inf")


def test_resolve_gate_trip_falls_back_to_clean_raw():
    # Smoothed chord grazes the exclusion (foreign copper far away) while
    # the raw staircase stays clear: emit the raw path, no failure.
    from kicad_autorouter.routing.route_raw import _resolve_gate_trip

    grid = _BoardGrid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])
    model = _smd_model(0.65, 2.0, "X2")
    raw = SimpleNamespace(branches=(((0, 7, 0), (12, 7, 0)),),
                           arc_pieces=())
    smoothed = SimpleNamespace(branches=(((0, 5, 0), (12, 5, 0)),),
                               arc_pieces=((("seg", 0.0, 5.0, 12.0, 5.0, 0),),))
    violations = fitted_pieces_violate(
        branches=list(smoothed.branches),
        arc_pieces=list(smoothed.arc_pieces),
        blocked_mask=blocked, grid=grid)
    assert violations  # the smoothed chord trips the gate
    result, failure, fell_back = _resolve_gate_trip(
        raw, smoothed, violations, blocked, grid, model, "X1", 0.125)
    assert failure is None
    assert fell_back
    assert result is raw


def test_resolve_gate_trip_touch_stays_fatal():
    # Smoothed copper over foreign pad copper: loud failure, no fallback.
    from kicad_autorouter.routing.route_raw import _resolve_gate_trip

    grid = _BoardGrid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])
    model = _smd_model(0.65, 0.55, "X2")
    raw = SimpleNamespace(branches=(((0, 7, 0), (12, 7, 0)),),
                           arc_pieces=())
    smoothed = SimpleNamespace(branches=(((0, 5, 0), (12, 5, 0)),),
                               arc_pieces=((("seg", 0.0, 5.0, 12.0, 5.0, 0),),))
    violations = fitted_pieces_violate(
        branches=list(smoothed.branches),
        arc_pieces=list(smoothed.arc_pieces),
        blocked_mask=blocked, grid=grid)
    assert violations
    result, failure, fell_back = _resolve_gate_trip(
        raw, smoothed, violations, blocked, grid, model, "X1", 0.125)
    assert result is None
    assert not fell_back
    assert failure.startswith("emit gate:")
    assert "failing loudly" in failure


def test_resolve_gate_trip_raw_also_trips_stays_fatal():
    # Graze on the smoothed path but the raw path trips too: still fatal.
    from kicad_autorouter.routing.route_raw import _resolve_gate_trip

    grid = _BoardGrid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])
    model = _smd_model(0.65, 2.0, "X2")
    raw = SimpleNamespace(branches=(((0, 5, 0), (12, 5, 0)),),
                           arc_pieces=())
    smoothed = SimpleNamespace(branches=(((0, 5, 0), (12, 5, 0)),),
                               arc_pieces=((("seg", 0.0, 5.0, 12.0, 5.0, 0),),))
    violations = fitted_pieces_violate(
        branches=list(smoothed.branches),
        arc_pieces=list(smoothed.arc_pieces),
        blocked_mask=blocked, grid=grid)
    assert violations
    result, failure, fell_back = _resolve_gate_trip(
        raw, smoothed, violations, blocked, grid, model, "X1", 0.125)
    assert result is None
    assert not fell_back
    assert failure.startswith("emit gate:")
