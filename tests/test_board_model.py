"""Tests for board_model against the DCCF.sved and minimal fixtures."""

from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    BoardRegion,
    Footprint,
    NetConnection,
    Point,
    _assign_board_regions,
    _extract_board_polygons,
    _parse_version,
    _polygon_bbox,
    board_model,
    edge_arcs,
    edge_cuts,
    footprints,
    keepout_zones,
    netlist,
    transform_local,
)
from kicad_autorouter.sexpr import SExpr, parse, parse_file

FIXTURES = Path(__file__).parent / "fixtures"
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"
TUBE111_PCB = FIXTURES / "tube111.kicad_pcb"
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
        # local (1, 2) angle=0 -> board (10+1, 20+2) = (11, 22)
        assert_point(transform_local(fp, 1.0, 2.0), 11.0, 22.0)

    def test_rotation_90_ccw(self):
        fp = _fp(angle_deg=90.0)
        # local (1, 0) rotate -90 in Y-down -> (0, -1) in board Y-up
        assert_point(transform_local(fp, 1.0, 0.0), 0.0, -1.0)
        # local (0, -3) rotate -90 -> (-3, 0)
        assert_point(transform_local(fp, 0.0, -3.0), -3.0, 0.0)

    def test_bottom_layer_mirror(self):
        fp = _fp(layer="B.Cu")
        # local (1, 2) -> B.Cu mirror X -> (-1, 2) -> board (-1, 2)
        assert_point(transform_local(fp, 1.0, 2.0), -1.0, 2.0)

    def test_bottom_layer_mirror_then_rotate(self):
        # mirror (1,0) -> (-1,0), then rotate -90 in Y-down: (-1,0) -> (0,1)
        fp = _fp(angle_deg=90.0, layer="B.Cu")
        assert_point(transform_local(fp, 1.0, 0.0), 0.0, 1.0)


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

    def test_dccf_has_no_locked_footprints(self, dccf_tree):
        assert all(not fp.locked for fp in footprints(dccf_tree))

    def test_aggregate_preserves_lock_state(self, minimal_tree):
        model = board_model(minimal_tree)
        assert model.by_ref["MH4"].locked is True
        assert model.by_ref["MH1"].locked is False

    def test_default_unlocked(self):
        assert _fp().locked is False


class TestUuid:
    def test_minimal_uuids(self, minimal_tree):
        by_ref = {fp.ref: fp for fp in footprints(minimal_tree)}
        assert by_ref["MH4"].uuid == "3f297a53-76ff-4e4a-a0f3-00c7aac0a1fd"
        assert by_ref["MH1"].uuid == "5c0af984-49c4-40a0-95aa-bb612ff4098b"

    def test_dccf_uuids_all_unique(self, dccf_tree):
        uuids = [fp.uuid for fp in footprints(dccf_tree)]
        assert len(uuids) == 97
        assert all(u for u in uuids)
        assert len(set(uuids)) == 97

    def test_default_empty_uuid(self):
        assert _fp().uuid == ""


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


class TestBoardRegions:
    """Tests for multi-board region detection and assignment."""

    def test_extract_board_polygons_dccf(self, dccf_tree):
        """DCCF has two disconnected board outlines -> 2 regions."""
        edge_cuts_list = edge_cuts(dccf_tree)
        edge_arcs_list = edge_arcs(dccf_tree)
        polygons = _extract_board_polygons(edge_cuts_list, edge_arcs_list)

        assert len(polygons) == 2

        # Region 0: left board (~44.7-144.7, 41.3-241.3)
        # Region 1: right board (~175.5-275.5, 41.3-241.3)
        bboxes = [_polygon_bbox(poly) for poly in polygons]
        # Check we have two distinct x-ranges (left and right)
        x_ranges = [(bbox[0], bbox[1]) for bbox in bboxes]
        assert any(xmin < 100 for xmin, _ in x_ranges)  # left region
        assert any(xmax > 150 for _, xmax in x_ranges)  # right region

    def test_extract_board_polygons_tube111(self):
        """tube111 has two disconnected board outlines -> 2 regions."""
        tube111_tree = parse_file(TUBE111_PCB)
        edge_cuts_list = edge_cuts(tube111_tree)
        edge_arcs_list = edge_arcs(tube111_tree)
        polygons = _extract_board_polygons(edge_cuts_list, edge_arcs_list)

        assert len(polygons) == 2

        # Region 0: rectangular (34.55-64.55, 27.25-155.75)
        # Region 1: complex with rounded corners (75.55-143.55, 39.25-143.75)
        bboxes = [_polygon_bbox(poly) for poly in polygons]
        x_ranges = [(bbox[0], bbox[1]) for bbox in bboxes]
        assert any(xmin < 70 for xmin, _ in x_ranges)  # left region
        assert any(xmax > 70 for _, xmax in x_ranges)  # right region

    def test_assign_regions(self, dccf_tree):
        """Footprints are assigned to correct board region by centroid."""
        edge_cuts_list = edge_cuts(dccf_tree)
        edge_arcs_list = edge_arcs(dccf_tree)
        polygons = _extract_board_polygons(edge_cuts_list, edge_arcs_list)

        fps = footprints(dccf_tree)
        assignment = _assign_board_regions(fps, polygons)

        # All 97 footprints with UUID should be assigned
        uuids_with_assignment = [fp.uuid for fp in fps if fp.uuid and fp.uuid in assignment]
        assert len(uuids_with_assignment) == 97

        # Check assignment values are 0, 1, or -1 (unassigned)
        for region_idx in assignment.values():
            assert region_idx in (0, 1, -1)

        # Verify at least some footprints in each region
        region_counts = {0: 0, 1: 0, -1: 0}
        for region_idx in assignment.values():
            region_counts[region_idx] = region_counts.get(region_idx, 0) + 1
        assert region_counts[0] > 0
        assert region_counts[1] > 0

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
        """tube111 BoardModel has 2 regions with footprints assigned."""
        tube111_tree = parse_file(TUBE111_PCB)
        model = board_model(tube111_tree)

        assert len(model.board_regions) == 2
        # Some footprints may be unassigned (region -1) due to complex polygon
        assigned = sum(1 for v in model.footprint_region.values() if v >= 0)
        assert assigned > 0
        assert len(model.footprint_region) == len([fp for fp in model.footprints if fp.uuid])


class TestVersionDetection:
    """Tests for KiCad version detection and warning logic."""

    def test_parse_version_v8(self):
        version, warning = _parse_version("8.0")
        assert version == "8.0"
        assert warning is None

    def test_parse_version_v9(self):
        version, warning = _parse_version("9.0")
        assert version == "9.0"
        assert warning is None

    def test_parse_version_v10(self):
        version, warning = _parse_version("10.0")
        assert version == "10.0"
        assert warning is None

    def test_parse_version_v11_warning(self):
        version, warning = _parse_version("11.0")
        assert version == "11.0"
        assert warning is not None
        assert "11" in warning
        assert "compatibility not verified" in warning

    def test_parse_version_v12_warning(self):
        version, warning = _parse_version("12.0")
        assert version == "12.0"
        assert warning is not None
        assert "12" in warning

    def test_parse_version_invalid(self):
        version, warning = _parse_version("not_a_version")
        assert version == "not_a_version"
        assert warning is None

    def test_parse_version_none(self):
        version, warning = _parse_version(None)
        assert version == ""
        assert warning is None

    def test_parse_version_empty(self):
        version, warning = _parse_version("")
        assert version == ""
        assert warning is None

    def test_board_model_version_dccf(self, dccf_tree):
        """DCCF fixture has generator_version 10.0."""
        model = board_model(dccf_tree)
        assert model.version == "10.0"
        assert model.version_warning is None

    def test_board_model_version_minimal(self, minimal_tree):
        """Minimal fixture has generator_version 9.0."""
        model = board_model(minimal_tree)
        assert model.version == "9.0"
        assert model.version_warning is None

    def test_board_model_version_tube111(self):
        """tube111 fixture has generator_version 10.0."""
        tube111_tree = parse_file(TUBE111_PCB)
        model = board_model(tube111_tree)
        assert model.version == "10.0"
        assert model.version_warning is None

    def test_board_model_v11_warning(self):
        """Custom tree with generator_version 11.0 should produce warning."""
        tree = parse("""(kicad_pcb
            (version 20241229)
            (generator "pcbnew")
            (generator_version "11.0")
        )""")
        model = board_model(tree)
        assert model.version == "11.0"
        assert model.version_warning is not None
        assert "compatibility not verified" in model.version_warning

    def test_board_model_no_generator_version(self):
        """Tree without generator_version should have empty version."""
        tree = parse("""(kicad_pcb
            (version 20241229)
            (generator "pcbnew")
        )""")
        model = board_model(tree)
        assert model.version == ""
        assert model.version_warning is None
