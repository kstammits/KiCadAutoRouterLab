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
    _to_board,
    _to_local,
    apply_deltas,
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
        # No mirroring: B.Cu uses the same transform as F.Cu (verified
        # against pcbnew on tube111 D6/D7, 2026-10-08).
        # local (1, 2) -> board (1, 2)
        assert_point(transform_local(fp, 1.0, 2.0), 1.0, 2.0)

    def test_bottom_layer_mirror_then_rotate(self):
        # No mirror; rotate -90 in Y-down: (1,0) -> (0,-1)
        fp = _fp(angle_deg=90.0, layer="B.Cu")
        assert_point(transform_local(fp, 1.0, 0.0), 0.0, -1.0)


class TestToLocalRoundTrip:
    @pytest.mark.parametrize("angle", [0.0, 90.0, 180.0, 270.0, 25.454, -33.3])
    @pytest.mark.parametrize("local", [(1.0, 2.0), (5.0, 0.0), (0.0, 0.0), (-3.1, 4.7)])
    def test_round_trip_all_angles(self, angle, local):
        """_to_local must be the exact inverse of _to_board at every angle.

        Regression: it used to apply the same R(-angle) as the forward
        transform, corrupting pads/courtyards by up to ~12mm for 90/270
        footprints on every Accept (measured 11.7mm on DCCF SW6).
        """
        fp = _fp(x=204.1, y=90.2, angle_deg=angle)
        lx, ly = local
        b = _to_board(lx, ly, fp.x_mm, fp.y_mm, angle, fp.layer)
        back = _to_local(b.x_mm, b.y_mm, fp.x_mm, fp.y_mm, angle, fp.layer)
        assert_point(back, lx, ly)


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
                # Pad absolute orientation must agree too (footprint rotation
                # advances both the model and the file pad angles).
                assert pa.angle_deg == pytest.approx(pb.angle_deg)
            for (a1, a2), (b1, b2) in zip(a.courtyard, b.courtyard):
                assert a1.x_mm == pytest.approx(b1.x_mm)
                assert a1.y_mm == pytest.approx(b1.y_mm)
                assert a2.x_mm == pytest.approx(b2.x_mm)
                assert a2.y_mm == pytest.approx(b2.y_mm)


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


class TestNetclasses:
    def test_parse_netclass_declarations(self):
        from kicad_autorouter.board_model import netclasses, netclass_trace_width
        from kicad_autorouter.sexpr import parse
        tree = parse(
            '(kicad_pcb (net_class "Power" (clearance 0.3) (trace_width 0.6)'
            ' (via_dia 0.8) (via_drill 0.4) (add_net "+12V") (add_net "GND"))'
            ' (net_class "Signals" (add_net "SDA")))'
        )
        classes, net_map = netclasses(tree)
        assert len(classes) == 2
        power = next(c for c in classes if c.name == "Power")
        assert power.clearance_mm == 0.3
        assert power.trace_width_mm == 0.6
        assert net_map["+12V"] == "Power"
        assert net_map["SDA"] == "Signals"
        # Defaults for missing numeric fields
        signals = next(c for c in classes if c.name == "Signals")
        assert signals.clearance_mm == 0.2
        assert signals.trace_width_mm == 0.25

    def test_fixtures_without_netclasses(self, minimal_tree):
        from kicad_autorouter.board_model import netclasses
        classes, net_map = netclasses(minimal_tree)
        assert classes == ()
        assert net_map == {}

    def test_model_carries_netclasses(self, minimal_tree):
        model = board_model(minimal_tree)
        assert model.net_classes == ()
        assert model.net_netclass == {}


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
        """tube111 has three disconnected board outlines -> 3 regions."""
        tube111_tree = parse_file(TUBE111_PCB)
        edge_cuts_list = edge_cuts(tube111_tree)
        edge_arcs_list = edge_arcs(tube111_tree)
        polygons = _extract_board_polygons(edge_cuts_list, edge_arcs_list)

        assert len(polygons) == 3

        # Region 0: left rectangular (34.55-64.55, 27.25-155.75)
        # Region 1: middle complex with rounded corners (75.55-103.55, 39.25-143.75)
        # Region 2: right complex with rounded corners (115.55-143.55, 39.25-129.65)
        bboxes = [_polygon_bbox(poly) for poly in polygons]
        x_ranges = [(bbox[0], bbox[1]) for bbox in bboxes]
        assert any(xmin < 70 for xmin, _ in x_ranges)  # left region
        assert any(70 < xmin < 110 for xmin, _ in x_ranges)  # middle region
        assert any(xmin > 110 for xmin, _ in x_ranges)  # right region

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
        """tube111 BoardModel has 3 regions with footprints assigned."""
        tube111_tree = parse_file(TUBE111_PCB)
        model = board_model(tube111_tree)

        assert len(model.board_regions) == 3
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


class TestGhostFootprints:
    """Tests for ghost/pseudo footprint parsing."""

    def test_ghost_footprint_parsed(self, ghost_test_tree):
        """Ghost footprints are detected and separated."""
        model = board_model(ghost_test_tree)

        # Should have 2 real footprints and 2 ghosts
        assert len(model.footprints) == 2
        assert len(model.ghost_footprints) == 2

        # Check real footprints
        refs = {fp.ref for fp in model.footprints}
        assert refs == {"U1", "R2"}

        # Check rail ghost
        rail_ghost = next(fp for fp in model.ghost_footprints if fp.ref == "GHOST_RAIL_TOP")
        assert rail_ghost.uuid == "ghost-rail-top-001"
        assert rail_ghost.ghost is True
        assert rail_ghost.x_mm == 50.0
        assert rail_ghost.y_mm == 10.0

        # Check ferrule ghost
        ferrule_ghost = next(fp for fp in model.ghost_footprints if fp.ref == "GHOST_FERRULE")
        assert ferrule_ghost.uuid == "ghost-ferrule-001"
        assert ferrule_ghost.ghost is True
        assert ferrule_ghost.x_mm == 50.0
        assert ferrule_ghost.y_mm == 20.0
        assert len(ferrule_ghost.ghost_attractions) == 1
        target_ref, ideal_len, ka = ferrule_ghost.ghost_attractions[0]
        assert target_ref == "U1"
        assert ideal_len == 25.0
        assert ka == 0.1

    def test_ghost_courtyard_parsed(self, ghost_test_tree):
        """Ghost footprint courtyard is parsed correctly."""
        model = board_model(ghost_test_tree)
        rail_ghost = next(fp for fp in model.ghost_footprints if fp.ref == "GHOST_RAIL_TOP")

        # Should have courtyard segments (4 lines forming a rectangle)
        assert len(rail_ghost.courtyard) == 4
        # Check courtyard bounds (rectangle from -30,-5 to 30,5 relative to ghost position)
        # Ghost at (50, 10), so courtyard spans x: 20-80, y: 5-15
        xs = [p.x_mm for seg in rail_ghost.courtyard for p in seg]
        ys = [p.y_mm for seg in rail_ghost.courtyard for p in seg]
        assert min(xs) == pytest.approx(20.0)
        assert max(xs) == pytest.approx(80.0)
        assert min(ys) == pytest.approx(5.0)
        assert max(ys) == pytest.approx(15.0)

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


class TestBackLayerNoMirror:
    """Regression: B.Cu footprints are NOT mirrored (tube111 D6/D7).

    Verified against pcbnew (2026-10-08): pad positions, courtyard extents
    and effective shapes match     ``pcbnew.LoadBoard`` exactly. The old code
    mirrored B.Cu local X, pushing asymmetric footprints outside their
    board region in the model/SVG while pcbnew showed them inside.
    """

    def test_d6_pad_positions_match_pcbnew(self, tube111_model):
        d6 = tube111_model.by_ref["D6"]
        assert d6.layer == "B.Cu"
        assert (d6.x_mm, d6.y_mm, d6.angle_deg) == (123.85, 41.125, 0.0)
        by_num = {p.number: p for p in d6.pads}
        assert_point(by_num["1"].position, 123.85, 41.125)
        # pcbnew: 134.01 (unmirrored 123.85 + 10.16; mirrored would be 113.69)
        assert_point(by_num["2"].position, 134.01, 41.125)

    def test_d7_pad_positions_match_pcbnew(self, tube111_model):
        d7 = tube111_model.by_ref["D7"]
        assert d7.layer == "B.Cu"
        assert (d7.x_mm, d7.y_mm, d7.angle_deg) == (138.05, 74.5, 180.0)
        by_num = {p.number: p for p in d7.pads}
        assert_point(by_num["1"].position, 138.05, 74.5)
        # pcbnew: 127.89 (138.05 - 10.16 under 180-deg rotation, no mirror)
        assert_point(by_num["2"].position, 127.89, 74.5)

    def test_d6_d7_courtyards_inside_region_2(self, tube111_model):
        region = tube111_model.board_regions[2]
        min_x, max_x, min_y, max_y = region.bbox
        assert (min_x, max_x, min_y, max_y) == pytest.approx(
            (115.55, 143.55, 39.25, 129.65)
        )
        for ref, x_lo, x_hi in (("D6", 122.5, 135.36), ("D7", 126.54, 139.4)):
            fp = tube111_model.by_ref[ref]
            xs = [p.x_mm for seg in fp.courtyard for p in seg]
            ys = [p.y_mm for seg in fp.courtyard for p in seg]
            assert min(xs) == pytest.approx(x_lo, abs=1e-6)
            assert max(xs) == pytest.approx(x_hi, abs=1e-6)
            assert min(xs) >= min_x and max(xs) <= max_x
            assert min(ys) >= min_y and max(ys) <= max_y

    def test_rotated_back_footprints_match_pcbnew(self, tube111_model):
        # Spot-check 90/-90-deg B.Cu placements (positions verified in pcbnew).
        expectations = {
            # ref: ((origin), {pad_num: (x, y)})
            "Q1": ((129.68, 93.27, 90.0),
                   {"1": (130.63, 94.345), "2": (128.73, 94.345),
                    "3": (129.68, 92.195)}),
            "R3": ((97.1, 46.3, 90.0),
                   {"1": (97.1, 47.3), "2": (97.1, 45.3)}),
            "C9": ((88.95, 132.325, -90.0),
                   {"1": (88.95, 131.2875), "2": (88.95, 133.3625)}),
        }
        for ref, (origin, pads) in expectations.items():
            fp = tube111_model.by_ref[ref]
            assert (fp.x_mm, fp.y_mm, fp.angle_deg) == pytest.approx(origin)
            by_num = {p.number: p for p in fp.pads}
            for num, (x, y) in pads.items():
                assert_point(by_num[num].position, x, y)
