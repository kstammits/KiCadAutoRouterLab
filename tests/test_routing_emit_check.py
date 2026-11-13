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


pytestmark = pytest.mark.unit


class _Grid:
    def __init__(self, origin=(0.0, 0.0), res=0.1):
        self.origin_mm = origin
        self.resolution_mm = res


def _masks(blocked_cells, shape=(2, 30, 30)):
    blocked = np.zeros(shape, dtype=bool)
    for l, r, c in blocked_cells:
        blocked[l, r, c] = True
    return blocked


@pytest.mark.unit
@pytest.mark.parametrize("row", [1, 7])
def test_straight_chords_pass(row):
    # A clean chord (row 1, no fitted pieces) and a chord one full cell
    # clear of exclusion (row 7, a toll road not a short) both pass.
    grid = _Grid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])
    pieces = [()] if row == 1 else [[("seg", 0.0, 7.0, 12.0, 7.0, 0)]]
    assert fitted_pieces_violate(
        branches=[[(0, row, 0), (12, row, 0)]],
        arc_pieces=pieces,
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


@pytest.mark.unit
@pytest.mark.parametrize("kind,expect_touch,expect_margin", [
    ("touch", True, 0.0),  # centerline over foreign pad copper
    ("graze", False, 0.825),  # foreign copper well clear of the edge
    ("own_pad", False, float("inf")),  # terminals sit on own copper
    ("foreign_via", True, None),  # foreign via annulus is a real touch
    ("own_via", False, float("inf")),  # own-net vias excluded like pads
])
def test_gate_touch_margin_table(kind, expect_touch, expect_margin):
    grid = _BoardGrid()
    if kind == "touch":
        model = _smd_model(0.65, 0.55, "X2")
    elif kind == "graze":
        model = _smd_model(0.65, 2.0, "X2")
    elif kind == "own_pad":
        model = _smd_model(0.65, 0.55, "X1")
    else:
        net = "X2" if kind == "foreign_via" else "X1"
        model = SimpleNamespace(
            footprints=(), copper_layers=("F.Cu", "B.Cu"),
            vias=(Via(position=Point(0.65, 0.55), size_mm=0.8, drill_mm=0.4,
                      layers=("F.Cu", "B.Cu"), net_name=net),),
        )
    is_touch, margin = gate_touch_margin(
        [[(0, 5, 0), (12, 5, 0)]], [()], grid, model, "X1", 0.125)
    assert bool(is_touch) == expect_touch
    if expect_margin is not None:
        if expect_margin == float("inf"):
            assert margin == float("inf")
        else:
            assert margin == pytest.approx(expect_margin, abs=1e-6)


@pytest.mark.unit
def test_gate_touch_margin_flags_rule_dirty_graze():
    # Track edge at y=0.675 (row 5 + half width 0.125); 1x1 pad centered
    # at y=1.325 leaves a 0.15mm edge gap — gate-clean at clearance 0.0
    # but rule-dirty at the 0.21 default. The tube116 grazes sit here.
    grid = _BoardGrid()
    model = _smd_model(0.65, 1.325, "X2")
    is_touch, margin = gate_touch_margin(
        [[(0, 5, 0), (12, 5, 0)]], [()], grid, model, "X1", 0.125)
    assert not is_touch
    assert margin == pytest.approx(0.15, abs=1e-6)
    is_touch, margin = gate_touch_margin(
        [[(0, 5, 0), (12, 5, 0)]], [()], grid, model, "X1", 0.125,
        clearance_mm=0.21)
    assert is_touch
    assert margin == pytest.approx(0.15, abs=1e-6)


@pytest.mark.unit
def test_resolve_gate_trip_rule_dirty_graze_fails_loudly():
    # Same 0.15mm graze through a tripped raster cell: at rule clearance
    # it must fail loudly, not fall back to raw (raw shares the hole).
    from kicad_autorouter.routing.route_raw import _resolve_gate_trip

    grid = _BoardGrid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])
    model = _smd_model(0.65, 1.325, "X2")
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
        raw, smoothed, violations, blocked, grid, model, "X1", 0.125,
        clearance_mm=0.21)
    assert result is None
    assert not fell_back
    assert failure.startswith("emit gate:")


@pytest.mark.unit
@pytest.mark.parametrize("case", [
    # Graze on smoothed, clean raw: emit the raw path, no failure.
    dict(name="graze_fallback", copper_y=2.0, raw_row=7,
         expect_fallback=True),
    # Smoothed copper over foreign pad copper: loud failure, no fallback.
    dict(name="touch_fatal", copper_y=0.55, raw_row=7,
         expect_fallback=False, expect_loud=True),
    # Graze on smoothed but the raw path trips too: still fatal.
    dict(name="raw_trips_fatal", copper_y=2.0, raw_row=5,
         expect_fallback=False, expect_loud=False),
])
def test_resolve_gate_trip_table(case):
    from kicad_autorouter.routing.route_raw import _resolve_gate_trip

    grid = _BoardGrid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])
    model = _smd_model(0.65, case["copper_y"], "X2")
    raw = SimpleNamespace(branches=(((0, case["raw_row"], 0),
                                     (12, case["raw_row"], 0)),),
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
    if case["expect_fallback"]:
        assert failure is None
        assert fell_back
        assert result is raw
    else:
        assert result is None
        assert not fell_back
        assert failure.startswith("emit gate:")
        if case["expect_loud"]:
            assert "failing loudly" in failure


def test_resolve_gate_trip_rescues_endpoint_dust():
    # Same raster trip as graze_fallback, but the tripped cells sit in the
    # routed net's own pad halo (own 1x1 pad at (0.35, 0.0) touches the
    # row-5 trip cells) and the full-class margin clears 0.2mm — pure
    # endpoint raster dust, so the smoothed chord is stamped as-is.
    from kicad_autorouter.routing.route_raw import _resolve_gate_trip

    grid = _BoardGrid()
    blocked = _masks([(0, 5, c) for c in range(3, 9)])

    def _fp(ref, x, y, net):
        return Footprint(
            ref=ref, footprint_id="test:fp", layer="F.Cu",
            x_mm=x, y_mm=y, angle_deg=0.0,
            pads=(Pad(number="1", net_name=net,
                      position=Point(x, y), size_mm=(1.0, 1.0),
                      shape="rect", angle_deg=0.0, pad_type="smd",
                      drill_mm=0.0, layers=("F.Cu",)),),
        )

    model = SimpleNamespace(
        footprints=(_fp("U1", 0.35, 0.0, "X1"), _fp("U2", 0.65, 2.0, "X2")),
        vias=(), copper_layers=("F.Cu", "B.Cu"))
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
        raw, smoothed, violations, blocked, grid, model, "X1", 0.125,
        clearance_mm=0.2)
    assert failure is None
    assert not fell_back
    assert result is smoothed


def test_resolve_gate_trip_mid_route_dust_still_falls_back():
    # Same trip cells, but no own pad nearby (foreign pad only, far away):
    # mid-route dust keeps the old fallback behavior so flows that assume
    # raw geometry (shove gap bookkeeping) are untouched.
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
    assert violations
    result, failure, fell_back = _resolve_gate_trip(
        raw, smoothed, violations, blocked, grid, model, "X1", 0.125,
        clearance_mm=0.2)
    assert failure is None
    assert fell_back
    assert result is raw
