"""Tests for board_model against the DCCF.sved and minimal fixtures."""

from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    BoardRegion,
    Footprint,
    NetConnection,
    Point,
    apply_deltas,
    board_model,
    edge_arcs,
    edge_cuts,
    footprints,
    keepout_zones,
    netlist,
    transform_local,
    zones,
)
from kicad_autorouter.sexpr import SExpr, parse, parse_file

FIXTURES = Path(__file__).parent / "fixtures"
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"
TUBE111_PCB = FIXTURES / "tube111.kicad_pcb"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"
GHOST_TEST_PCB = FIXTURES / "ghost_test.kicad_pcb"


@pytest.fixture(scope="module")
def tube111_model():
    return board_model(parse_file(TUBE111_PCB))


@pytest.fixture(scope="module")
def ghost_test_tree():
    return parse_file(GHOST_TEST_PCB)


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


def _sw6(dccf_tree):
    return next(fp for fp in footprints(dccf_tree) if fp.ref == "SW6")


class TestApplyDeltasMatchesNudge:
    def test_model_matches_reparsed_tree_all_angles(self, dccf_tree):
        """apply_deltas must agree with nudge_footprint_by_uuid + re-parse.

        The preview renders the apply_deltas model and Accept writes the
        nudged tree; any divergence shows the part in one spot and lands it
        in another (the C11/R7 report).
        """
        from kicad_autorouter.io import nudge_footprint_by_uuid

        m = board_model(dccf_tree)
        deltas = {
            fp.uuid: (1.5, -2.25, 25.0)
            for fp in m.footprints
            if fp.uuid and not fp.locked
        }
        moved = apply_deltas(m, deltas)
        tree = dccf_tree
        for uuid, (dx, dy, da) in deltas.items():
            tree = nudge_footprint_by_uuid(tree, uuid, dx, dy, da)
        reparsed = board_model(tree)

        by_uuid_moved = {fp.uuid: fp for fp in moved.footprints}
        by_uuid_reparsed = {fp.uuid: fp for fp in reparsed.footprints}
        assert set(by_uuid_moved) == set(by_uuid_reparsed)
        for uuid, a in by_uuid_moved.items():
            b = by_uuid_reparsed[uuid]
            assert a.x_mm == pytest.approx(b.x_mm)
            assert a.y_mm == pytest.approx(b.y_mm)
            assert (a.angle_deg - b.angle_deg) == pytest.approx(0.0)
            for pa, pb in zip(a.pads, b.pads):
                assert pa.position.x_mm == pytest.approx(pb.position.x_mm)
                assert pa.position.y_mm == pytest.approx(pb.position.y_mm)
                assert pa.angle_deg == pytest.approx(pb.angle_deg)
            for (a1, a2), (b1, b2) in zip(a.courtyard, b.courtyard):
                assert a1.x_mm == pytest.approx(b1.x_mm)
                assert a1.y_mm == pytest.approx(b1.y_mm)
                assert a2.x_mm == pytest.approx(b2.x_mm)
                assert a2.y_mm == pytest.approx(b2.y_mm)


class TestFootprints:
    def test_pads_in_board_coordinates(self, dccf_tree):
        # pad "1" local (-3, 0) rotated -90 about (267.5, 206.5)
        # x=-3, y=0, rad=-90: bx = 267.5 + (-3)*0 - 0*(-1) = 267.5
        # by = 206.5 + (-3)*(-1) + 0*0 = 209.5
        sw6 = _sw6(dccf_tree)
        pad1 = next(p for p in sw6.pads if p.number == "1")
        assert_point(pad1.position, 267.5, 209.5)

    def test_courtyard_in_board_coordinates(self, dccf_tree):
        # local (6.2, -2.9) rotate -90 in Y-down:
        # x=6.2, y=-2.9, rad=-90, cos=0, sin=-1
        # bx = 267.5 + 6.2*0 - (-2.9)*(-1) = 267.5 - 2.9 = 264.6
        # by = 206.5 + 6.2*(-1) + (-2.9)*0 = 206.5 - 6.2 = 200.3
        sw6 = _sw6(dccf_tree)
        assert len(sw6.courtyard) >= 4
        endpoints = [p for seg in sw6.courtyard for p in seg]
        assert any(
            p.x_mm == pytest.approx(264.6) and p.y_mm == pytest.approx(200.3)
            for p in endpoints
        )


class TestLockState:
    def test_minimal_locks(self, minimal_tree):
        # MH4/MH2/MH3 carry a top-level (locked yes) token; MH1 does not
        by_ref = {fp.ref: fp for fp in footprints(minimal_tree)}
        assert by_ref["MH4"].locked is True
        assert by_ref["MH2"].locked is True
        assert by_ref["MH3"].locked is True
        assert by_ref["MH1"].locked is False

    def test_dccf_has_locked_footprints(self, dccf_tree):
        locked = [fp for fp in footprints(dccf_tree) if fp.locked]
        assert len(locked) == 33

    def test_aggregate_preserves_lock_state(self, minimal_tree):
        model = board_model(minimal_tree)
        assert model.by_ref["MH4"].locked is True
        assert model.by_ref["MH1"].locked is False


class TestUuid:
    def test_dccf_uuids_all_unique(self, dccf_tree):
        uuids = [fp.uuid for fp in footprints(dccf_tree)]
        assert len(uuids) == 97
        assert all(u for u in uuids)
        assert len(set(uuids)) == 97


class TestKeepoutZones:
    def test_dccf_has_no_keepout_zones(self, dccf_tree):
        # its single zone is a GND pour without a keepout block
        assert keepout_zones(dccf_tree) == []


class TestZoneFillRules:
    """Zone fill settings mirror the (zone ...) block for pour analysis."""

    def test_tube111_gnd_zone_fill_rules(self, tube111_model):
        gnd = [z for z in tube111_model.zones if z.net_name == "GND"]
        assert len(gnd) == 6
        by_uuid = {z.uuid: z for z in gnd}
        z = by_uuid["00000000-0000-0000-0000-0000612bf858"]
        assert z.layers == ("F.Cu",)
        assert z.clearance_mm == pytest.approx(0.508)
        assert z.min_thickness_mm == pytest.approx(0.254)
        assert z.thermal_gap_mm == pytest.approx(0.508)
        assert z.thermal_bridge_width_mm == pytest.approx(0.508)
        assert z.island_removal_mode == 0
        assert len(z.polygon) == 5

    def test_tube111_mixed_clearances(self, tube111_model):
        clearances = sorted({
            z.clearance_mm
            for z in tube111_model.zones
            if z.net_name == "GND"
        })
        assert clearances == pytest.approx([0.4, 0.508])

    def test_dccf_gnd_zone_spans_both_layers(self, dccf_tree):
        gnd = [z for z in zones(dccf_tree) if z.net_name == "GND"]
        assert len(gnd) == 1
        assert set(gnd[0].layers) == {"F.Cu", "B.Cu"}
        assert gnd[0].clearance_mm == pytest.approx(0.508)

    def test_keepout_zone_fill_defaults(self, minimal_tree):
        # keepout zones carry a (fill ...) block with thermals but their own
        # zero clearance; net stays unconnected.
        for z in zones(minimal_tree):
            assert z.net_name is None
            assert z.thermal_gap_mm == pytest.approx(0.508)
            assert z.thermal_bridge_width_mm == pytest.approx(0.508)
            assert z.clearance_mm == pytest.approx(0.0)
            assert z.min_thickness_mm == pytest.approx(0.254)


class TestNetlist:
    def test_dccf_net_count(self, dccf_tree):
        assert len(netlist(dccf_tree)) == 59

    def test_known_connection(self, dccf_tree):
        assert NetConnection("SW6", "1") in netlist(dccf_tree)["VR-"]

    def test_minimal_empty(self, minimal_tree):
        # mounting-hole pads carry no (net ...) references
        assert netlist(minimal_tree) == {}


class TestBoardModel:
    def test_by_ref_first_wins_on_duplicate_refs(self, dccf_tree):
        # masked fixture has duplicate refs (e.g. H6 x5); by_ref keeps the first
        model = board_model(dccf_tree)
        h6s = [fp for fp in model.footprints if fp.ref == "H6"]
        assert len(h6s) > 1
        assert model.by_ref["H6"] is h6s[0]
        assert "" not in model.by_ref


class TestBoardRegions:
    """Tests for multi-board region detection and assignment."""

    def test_board_model_has_regions(self, dccf_tree):
        """BoardModel exposes board_regions and footprint_region."""
        model = board_model(dccf_tree)

        assert len(model.board_regions) == 2
        for region in model.board_regions:
            assert isinstance(region, BoardRegion)
            assert region.uuid
            assert region.polygon
            assert region.bbox[0] < region.bbox[1]
            assert region.bbox[2] < region.bbox[3]

        # footprint_region maps uuid -> region_idx
        assert len(model.footprint_region) == 97
        for fp in model.footprints:
            if fp.uuid:
                assert fp.uuid in model.footprint_region
                assert model.footprint_region[fp.uuid] in (0, 1, -1)

    def test_tube111_board_model_has_regions(self):
        """tube111 BoardModel has 3 regions with footprints assigned."""
        tube111_tree = parse_file(TUBE111_PCB)
        model = board_model(tube111_tree)

        assert len(model.board_regions) == 3
        # Some footprints may be unassigned (region -1) due to complex polygon
        assigned = sum(1 for v in model.footprint_region.values() if v >= 0)
        assert assigned > 0
        assert len(model.footprint_region) == len([fp for fp in model.footprints if fp.uuid])


class TestGhostFootprints:
    """Tests for ghost/pseudo footprint parsing."""

    def test_ghost_excluded_from_by_ref(self, ghost_test_tree):
        """Ghost footprints are not in by_ref mapping."""
        model = board_model(ghost_test_tree)
        assert "GHOST_RAIL_TOP" not in model.by_ref
        assert "GHOST_FERRULE" not in model.by_ref
        assert "U1" in model.by_ref
        assert "R2" in model.by_ref

    def test_ghost_has_uuid(self, ghost_test_tree):
        """Ghost footprints have UUIDs."""
        model = board_model(ghost_test_tree)
        rail_ghost = next(fp for fp in model.ghost_footprints if fp.ref == "GHOST_RAIL_TOP")
        assert rail_ghost.uuid == "ghost-rail-top-001"
        assert rail_ghost.uuid in model.footprint_region
        ferrule_ghost = next(fp for fp in model.ghost_footprints if fp.ref == "GHOST_FERRULE")
        assert ferrule_ghost.uuid == "ghost-ferrule-001"
        assert ferrule_ghost.uuid in model.footprint_region