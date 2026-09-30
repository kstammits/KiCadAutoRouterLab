"""Tests for board_model against the DCCF.sved and minimal fixtures."""

from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    Footprint,
    NetConnection,
    Point,
    board_model,
    footprints,
    keepout_zones,
    netlist,
    transform_local,
)
from kicad_autorouter.sexpr import parse_file

FIXTURES = Path(__file__).parent / "fixtures"
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"


@pytest.fixture(scope="module")
def dccf_tree():
    return parse_file(DCCF_PCB)


@pytest.fixture(scope="module")
def minimal_tree():
    return parse_file(MINIMAL_PCB)


def _fp(x=0.0, y=0.0, angle_deg=0.0, layer="F.Cu", ref="X1"):
    return Footprint(
        ref=ref, footprint_id="", layer=layer, x_mm=x, y_mm=y, angle_deg=angle_deg
    )


def assert_point(p: Point, x: float, y: float):
    assert p.x_mm == pytest.approx(x)
    assert p.y_mm == pytest.approx(y)


class TestTransformLocal:
    def test_identity_top_layer(self):
        fp = _fp(x=10.0, y=20.0)
        assert_point(transform_local(fp, 1.0, 2.0), 11.0, 22.0)

    def test_rotation_90_ccw(self):
        fp = _fp(angle_deg=90.0)
        assert_point(transform_local(fp, 1.0, 0.0), 0.0, 1.0)
        assert_point(transform_local(fp, 0.0, -3.0), 3.0, 0.0)

    def test_bottom_layer_mirror(self):
        fp = _fp(layer="B.Cu")
        assert_point(transform_local(fp, 1.0, 2.0), -1.0, 2.0)

    def test_bottom_layer_mirror_then_rotate(self):
        # mirror (1,0) -> (-1,0), then rotate +90 CCW: (-1,0) -> (0,-1)
        fp = _fp(angle_deg=90.0, layer="B.Cu")
        assert_point(transform_local(fp, 1.0, 0.0), 0.0, -1.0)


def _sw6(dccf_tree):
    return next(fp for fp in footprints(dccf_tree) if fp.ref == "SW6")


class TestFootprints:
    def test_footprint_count_dccf(self, dccf_tree):
        assert len(footprints(dccf_tree)) == 97

    def test_known_footprint_position(self, dccf_tree):
        sw6 = _sw6(dccf_tree)
        assert (sw6.x_mm, sw6.y_mm, sw6.angle_deg, sw6.layer) == (
            267.5,
            206.5,
            90.0,
            "F.Cu",
        )

    def test_pads_in_board_coordinates(self, dccf_tree):
        # pad "1" local (-3, 0) rotated +90 about (267.5, 206.5) -> (267.5, 203.5)
        sw6 = _sw6(dccf_tree)
        pad1 = next(p for p in sw6.pads if p.number == "1")
        assert_point(pad1.position, 267.5, 203.5)

    def test_courtyard_in_board_coordinates(self, dccf_tree):
        # local (6.2, -2.9) rotated +90 -> board (270.4, 212.7)
        sw6 = _sw6(dccf_tree)
        assert len(sw6.courtyard) >= 4
        endpoints = [p for seg in sw6.courtyard for p in seg]
        assert any(
            p.x_mm == pytest.approx(270.4) and p.y_mm == pytest.approx(212.7)
            for p in endpoints
        )


class TestKeepoutZones:
    def test_minimal_zones(self, minimal_tree):
        zones = keepout_zones(minimal_tree)
        assert len(zones) == 2
        z = zones[0]
        assert [(p.x_mm, p.y_mm) for p in z.polygon] == [
            (215.0, 145.0),
            (55.0, 145.0),
            (55.0, 142.5),
            (215.0, 142.5),
        ]
        assert not z.tracks_allowed
        assert not z.vias_allowed
        assert not z.pads_allowed
        assert z.copper_pour_allowed
        assert not z.footprints_allowed
        assert set(z.layers) == {"F.Cu", "B.Cu"}

    def test_dccf_has_no_keepout_zones(self, dccf_tree):
        # its single zone is a GND pour without a keepout block
        assert keepout_zones(dccf_tree) == []


class TestNetlist:
    def test_dccf_net_count(self, dccf_tree):
        assert len(netlist(dccf_tree)) == 59

    def test_known_connection(self, dccf_tree):
        assert NetConnection("SW6", "1") in netlist(dccf_tree)["VR-"]

    def test_total_connections_match_pads_with_nets(self, dccf_tree):
        fps = footprints(dccf_tree)
        expected = sum(
            1 for fp in fps for p in fp.pads if p.net_name is not None
        )
        total = sum(len(conns) for conns in netlist(dccf_tree).values())
        assert total == expected

    def test_minimal_empty(self, minimal_tree):
        # mounting-hole pads carry no (net ...) references
        assert netlist(minimal_tree) == {}


class TestBoardModel:
    def test_aggregate_dccf(self, dccf_tree):
        model = board_model(dccf_tree)
        assert len(model.footprints) == 97
        assert_point(Point(model.by_ref["SW6"].x_mm, model.by_ref["SW6"].y_mm), 267.5, 206.5)

    def test_by_ref_first_wins_on_duplicate_refs(self, dccf_tree):
        # masked fixture has duplicate refs (e.g. H6 x5); by_ref keeps the first
        model = board_model(dccf_tree)
        h6s = [fp for fp in model.footprints if fp.ref == "H6"]
        assert len(h6s) > 1
        assert model.by_ref["H6"] is h6s[0]
        assert "" not in model.by_ref

    def test_aggregate_minimal(self, minimal_tree):
        model = board_model(minimal_tree)
        assert len(model.footprints) == 4
        assert len(model.keepout_zones) == 2
        assert model.nets == {}
