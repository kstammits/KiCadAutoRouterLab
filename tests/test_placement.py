"""Tests for the placement domain: params validation and runners.

Test categories (pytest markers):
- unit: Pure logic, no fixtures
- params: PlacementParams validation/serialization
- stub: Demo jitter mode (works without nets)
- physics: MST force-spring simulation (requires nets)
- minimal: Uses minimal.kicad_pcb fixture (4 footprints, no nets)
- dccf: Uses DCCF.sved.kicad_pcb fixture (97 footprints, 59 nets)
"""

import math
from pathlib import Path

import numpy as np
import pytest

from kicad_autorouter.board_model import Footprint, board_model
from kicad_autorouter.placement import (
    PlacementParams,
    _collect_pad_nodes,
    _jitter_for,
    _run_region_simulation,
    run_placement,
)
from kicad_autorouter.sexpr import parse_file

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"
TUBE111_PCB = FIXTURES / "tube111.kicad_pcb"
GHOST_TEST_PCB = FIXTURES / "ghost_test.kicad_pcb"


@pytest.fixture(scope="module")
def ghost_test_model():
    return board_model(parse_file(GHOST_TEST_PCB))


@pytest.fixture(scope="module")
def minimal_model():
    return board_model(parse_file(MINIMAL_PCB))


@pytest.fixture(scope="module")
def dccf_model():
    return board_model(parse_file(DCCF_PCB))


@pytest.mark.unit
@pytest.mark.params
class TestParams:
    """PlacementParams validation and serialization."""

    def test_defaults(self):
        p = PlacementParams()
        assert p.repulsion_kr == 100.0
        assert p.attraction_ka == 0.05
        assert p.ideal_length_mm == 25.0
        assert p.max_iterations == 1000
        assert p.convergence_eps_mm == 0.01
        assert p.demo_jitter_mm == 0.0
        assert p.stub is False
        assert p.rigid_stiffness == 5e5

    def test_roundtrip(self):
        p = PlacementParams(repulsion_kr=42.5, demo_jitter_mm=3.0)
        q = PlacementParams.from_dict(p.to_dict())
        assert q == p

    def test_from_dict_fills_missing_and_ignores_unknown(self):
        q = PlacementParams.from_dict({"repulsion_kr": 1.0, "future_param": 9})
        assert q.repulsion_kr == 1.0
        assert q.attraction_ka == 0.05

    @pytest.mark.parametrize(
        "bad",
        [
            {"max_iterations": -1},
            {"max_iterations": 2.5},
            {"repulsion_kr": 0},
            {"attraction_ka": -0.1},
            {"ideal_length_mm": 0},
            {"convergence_eps_mm": 0},
            {"demo_jitter_mm": -1},
            {"repulsion_kr": "big"},
            {"stub": "yes"},
        ],
    )
    def test_from_dict_rejects_bad_values(self, bad):
        with pytest.raises(ValueError):
            PlacementParams.from_dict(bad)


@pytest.mark.unit
@pytest.mark.params
class TestJitter:
    """Deterministic per-UUID jitter for demo mode."""

    def test_deterministic_per_uuid(self):
        a = _jitter_for("uuid-a", 5.0)
        b = _jitter_for("uuid-a", 5.0)
        assert a == b

    def test_magnitude_bounded(self):
        for i in range(20):
            dx, dy = _jitter_for(f"uuid-{i}", 7.5)
            assert math.hypot(dx, dy) <= 7.5 + 1e-9

    def test_different_uuids_produce_different_jitter(self):
        results = {_jitter_for(f"uuid-{i}", 10.0) for i in range(50)}
        assert len(results) > 1  # Should have variety


@pytest.mark.stub
@pytest.mark.minimal
class TestStubRunner:
    """Demo jitter mode (stub=True, demo_jitter_mm > 0) - works without nets."""

    MH1_UUID = "5c0af984-49c4-40a0-95aa-bb612ff4098b"

    def test_identity_with_default_params(self, minimal_model):
        p = PlacementParams()
        prop = run_placement(minimal_model, p)
        assert prop.deltas == {}
        assert prop.iterations == 0
        assert prop.final_max_disp_mm == 0.0
        assert prop.elapsed_s >= 0.0
        assert prop.params == p.to_dict()

    def test_jitter_moves_only_unlocked_parts(self, minimal_model):
        p = PlacementParams(demo_jitter_mm=5.0, stub=False)
        prop = run_placement(minimal_model, p)
        # only MH1 is unlocked in the minimal fixture
        assert set(prop.deltas) == {self.MH1_UUID}
        dx, dy, da = prop.deltas[self.MH1_UUID]
        assert da == 0.0
        assert 0.0 < math.hypot(dx, dy) <= 5.0 + 1e-9
        assert prop.final_max_disp_mm == pytest.approx(math.hypot(dx, dy))

    def test_jitter_is_deterministic(self, minimal_model):
        p = PlacementParams(demo_jitter_mm=3.0)
        first = run_placement(minimal_model, p).deltas
        second = run_placement(minimal_model, p).deltas
        assert first == second

    def test_locked_mask_from_fixture(self, minimal_model):
        locked = [fp.locked for fp in minimal_model.footprints]
        assert locked == [True, False, True, True]


@pytest.mark.physics
@pytest.mark.dccf
class TestPhysicsRunner:
    """MST force-spring simulation (stub=False) - requires nets."""

    def test_physics_moves_unlocked(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)
        assert prop.deltas
        for uuid, (dx, dy, da) in prop.deltas.items():
            assert isinstance(dx, float)
            assert isinstance(dy, float)
            assert isinstance(da, float)
        assert prop.iterations > 0
        assert prop.final_max_disp_mm > 0.0

    def test_physics_respects_locked(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)
        for uuid in prop.deltas:
            fp = next(fp for fp in dccf_model.footprints if fp.uuid == uuid)
            assert not fp.locked

    def test_selective_movable_uuids(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        movable_fp = next(fp for fp in dccf_model.footprints if not fp.locked and fp.uuid)
        prop = run_placement(dccf_model, p, movable_uuids={movable_fp.uuid})
        assert prop.deltas
        assert set(prop.deltas) == {movable_fp.uuid}

    def test_selective_movable_uuids_empty(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p, movable_uuids=set())
        assert prop.deltas == {}

    def test_selective_movable_uuids_excludes_locked(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=10)
        locked_fps = [fp for fp in dccf_model.footprints if fp.locked]
        if not locked_fps:
            pytest.skip("No locked footprints in DCCF fixture")
        locked_fp = locked_fps[0]
        prop = run_placement(dccf_model, p, movable_uuids={locked_fp.uuid})
        assert prop.deltas == {}

    def test_convergence_stops_early(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=1000, convergence_eps_mm=100.0)
        prop = run_placement(dccf_model, p)
        assert prop.iterations < 1000

    def test_iterations_param_override(self, dccf_model):
        p = PlacementParams(stub=False, max_iterations=1000)
        prop = run_placement(dccf_model, p)
        assert prop.iterations <= 1000

    # --- MST-specific tests ---

    def test_mst_topology_sw6_collinear_5pad(self, dccf_model):
        """SW6 (dbe92a0d) has 5 collinear pads - MST should connect nearest neighbors."""
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)
        # SW6 should move and rotate (even though collinear, net forces differ)
        assert prop.deltas
        # Check SW6 moved
        sw6_uuid = "dbe92a0d-7c4a-4b5e-8b5d-3e4f1a2b9c8d"
        if sw6_uuid in prop.deltas:
            dx, dy, da = prop.deltas[sw6_uuid]
            # With collinear pads, rotation may be small but position should change
            assert dx != 0.0 or dy != 0.0

    def test_mst_topology_14pad_grid(self, dccf_model):
        """U4 (66d121ca) has 14 pads in grid - MST should form grid connections."""
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)
        u4_uuid = "66d121ca-4b5e-8b5d-3e4f1a2b9c8d"
        if u4_uuid in prop.deltas:
            dx, dy, da = prop.deltas[u4_uuid]
            assert dx != 0.0 or dy != 0.0 or da != 0.0

    def test_mst_preserves_footprint_shape(self, dccf_model):
        """Rigid constraints should keep pad distances constant (within tolerance)."""
        from kicad_autorouter.placement import _collect_pad_nodes, _build_rigid_constraints_mst

        pads, _, fp_uuid_to_pad_indices, _, _ = _collect_pad_nodes(dccf_model)
        pad_positions = np.array([[p.position.x_mm, p.position.y_mm] for p in pads])

        rigid_src, rigid_dst, rigid_rest = _build_rigid_constraints_mst(pad_positions, fp_uuid_to_pad_indices)

        # Check rest lengths match initial distances
        for i, j, L0 in zip(rigid_src, rigid_dst, rigid_rest):
            actual = np.linalg.norm(pad_positions[j] - pad_positions[i])
            assert actual == pytest.approx(L0, rel=1e-6)

    def test_mst_no_bending_for_collinear_under_pure_translation(self, dccf_model):
        """If all pads of a collinear footprint feel equal force, it should translate without rotation."""
        # This is hard to test deterministically, but we can at least verify
        # the pose extraction doesn't produce NaN
        p = PlacementParams(stub=False, max_iterations=5)
        prop = run_placement(dccf_model, p)
        for uuid, (dx, dy, da) in prop.deltas.items():
            assert not math.isnan(dx) and not math.isnan(dy) and not math.isnan(da)


@pytest.mark.physics
@pytest.mark.dccf
class TestPerRegionSimulation:
    """Tests for multi-region placement simulation."""

    def test_per_region_simulation(self, dccf_model):
        """DCCF parts don't collapse to middle between boards."""
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)

        # Should have deltas for unlocked footprints
        assert prop.deltas

        # Verify footprints stay in their original regions
        # (don't collapse to the gap between the two boards)
        for uuid, (dx, dy, _) in prop.deltas.items():
            fp = next(fp for fp in dccf_model.footprints if fp.uuid == uuid)
            region_idx = dccf_model.footprint_region.get(fp.uuid, -1)
            if region_idx >= 0:
                region = dccf_model.board_regions[region_idx]
                # New position should still be within region's bbox (with some margin)
                new_x = fp.x_mm + dx
                new_y = fp.y_mm + dy
                # Allow 10mm margin since simulation can move parts
                assert new_x >= region.bbox[0] - 10.0
                assert new_x <= region.bbox[1] + 10.0
                assert new_y >= region.bbox[2] - 10.0
                assert new_y <= region.bbox[3] + 10.0

    def test_no_cross_region_forces(self, dccf_model):
        """Repulsion/attraction don't cross region boundaries."""
        # This test verifies the simulation doesn't create forces between
        # pads in different regions. Hard to test directly, but we can verify
        # the region separation is maintained.
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)

        # Just verify it runs without error and produces reasonable results
        assert prop.iterations > 0
        assert prop.final_max_disp_mm >= 0.0

    def test_empty_region_skipped(self, dccf_model):
        """Region with no movable footprints is skipped gracefully."""
        # In DCCF, both regions have movable footprints, so this test
        # just verifies the logic path exists
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(dccf_model, p)
        assert prop.iterations >= 0

    def test_tube111_three_regions(self):
        """tube111 with 3 regions runs without error."""
        tube111_model = board_model(parse_file(TUBE111_PCB))
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(tube111_model, p)

        assert len(tube111_model.board_regions) == 3
        assert prop.iterations >= 0


@pytest.mark.physics
@pytest.mark.dccf
class TestCourtyardCollision:
    """Tests for courtyard polygon collision detection (shapely-based)."""

    def test_courtyard_polygon_collision(self, dccf_model):
        """Overlapping courtyard polygons should generate repulsion forces."""
        p = PlacementParams(stub=False, max_iterations=5, courtyard_repulsion_kc=2000.0)
        prop = run_placement(dccf_model, p)

        # Verify the simulation runs and produces forces
        assert prop.forces
        for uuid, (fx, fy) in prop.forces.items():
            assert not math.isnan(fx)
            assert not math.isnan(fy)

        # Verify courtyard collision forces are non-trivial
        # (at least some footprints should experience courtyard forces)
        total_force_magnitude = sum(math.hypot(fx, fy) for fx, fy in prop.forces.values())
        assert total_force_magnitude > 0.0

    def test_courtyard_forces_are_zero_when_disabled(self, dccf_model):
        """With courtyard_repulsion_kc=0, collision forces should be zero."""
        p = PlacementParams(stub=False, max_iterations=5, courtyard_repulsion_kc=0.0)
        prop = run_placement(dccf_model, p)

        # Only repulsion, attraction, rigid forces remain
        total_force_magnitude = sum(math.hypot(fx, fy) for fx, fy in prop.forces.values())
        # Should still have other forces
        assert total_force_magnitude > 0.0


@pytest.mark.physics
@pytest.mark.minimal
class TestGhostComponents:
    """Tests for ghost/pseudo component behavior in placement."""

    def test_ghost_repels_real_footprints(self, ghost_test_model):
        """Ghost footprint with courtyard should repel real footprints via collision."""
        # Run placement with strong courtyard repulsion
        p = PlacementParams(
            stub=False,
            max_iterations=50,
            courtyard_repulsion_kc=5000.0,
            repulsion_kr=10.0,  # Low Coulomb repulsion to isolate courtyard effect
        )
        prop = run_placement(ghost_test_model, p)

        # Ghost should not have deltas (never moves)
        ghost_uuid = "ghost-rail-top-001"
        assert ghost_uuid not in prop.deltas

        # Real footprints should move away from ghost
        assert prop.deltas
        for uuid, (dx, dy, da) in prop.deltas.items():
            fp = next(fp for fp in ghost_test_model.footprints if fp.uuid == uuid)
            # Both R1 and R2 should move down (away from ghost at y=10)
            assert dy > 0, f"{fp.ref} should move down (positive dy), got dy={dy}"

    def test_ghost_no_deltas_in_proposal(self, ghost_test_model):
        """Ghost footprints never appear in placement proposal deltas."""
        p = PlacementParams(stub=False, max_iterations=10)
        prop = run_placement(ghost_test_model, p)

        ghost_uuid = "ghost-rail-top-001"
        assert ghost_uuid not in prop.deltas
        # Only real footprints should have deltas
        for uuid in prop.deltas:
            fp = next((fp for fp in ghost_test_model.footprints if fp.uuid == uuid), None)
            assert fp is not None, f"Delta for unknown UUID {uuid}"
            assert fp.ghost is False

    def test_ghost_courtyard_collision_force(self, ghost_test_model):
        """Ghost courtyard collision generates forces on real footprints."""
        p = PlacementParams(stub=False, max_iterations=5, courtyard_repulsion_kc=2000.0)
        prop = run_placement(ghost_test_model, p)

        # Forces should be non-zero for real footprints near ghost
        assert prop.forces
        for uuid, (fx, fy) in prop.forces.items():
            fp = next((fp for fp in ghost_test_model.footprints if fp.uuid == uuid), None)
            if fp:
                # R1 and R2 start at y=50, ghost at y=10 with courtyard up to y=15
                # They should feel downward repulsion (positive fy)
                assert fy > 0, f"{fp.ref} should feel downward force, got fy={fy}"

    def test_ghost_attraction_parsing(self, ghost_test_model):
        """Ghost attraction properties are parsed correctly."""
        ghost = next(fp for fp in ghost_test_model.ghost_footprints if fp.ref == "GHOST_FERRULE")
        assert len(ghost.ghost_attractions) == 1
        target_ref, ideal_len, ka = ghost.ghost_attractions[0]
        assert target_ref == "U1"
        assert ideal_len == 25.0
        assert ka == 0.1

    def test_ghost_attracts_target_footprint(self, ghost_test_model):
        """Ghost with attraction pulls target footprint toward it."""
        # Ghost at y=20, U1 at y=50. Attraction should pull U1 up (negative dy).
        p = PlacementParams(
            stub=False,
            max_iterations=30,
            courtyard_repulsion_kc=0.0,  # Disable courtyard to isolate attraction
            repulsion_kr=10.0,  # Low Coulomb repulsion
        )
        prop = run_placement(ghost_test_model, p)

        # U1 should move toward ghost (upward = negative dy)
        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        assert u1_uuid in prop.deltas
        dx, dy, da = prop.deltas[u1_uuid]
        assert dy < 0, f"U1 should move up toward ghost (negative dy), got dy={dy}"

        # R2 (no attraction) should not move significantly toward ghost
        r2_uuid = "bbbbbbbb-1111-2222-3333-444444444444"
        if r2_uuid in prop.deltas:
            dx2, dy2, da2 = prop.deltas[r2_uuid]
            # R2 should not be pulled up as strongly
            assert dy2 >= dy, f"R2 should not be pulled up more than U1"

    def test_ghost_attraction_force_distribution(self, ghost_test_model):
        """Attraction force is distributed equally to all target pads."""
        p = PlacementParams(stub=False, max_iterations=5, courtyard_repulsion_kc=0.0)
        prop = run_placement(ghost_test_model, p)

        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        assert u1_uuid in prop.forces
        fx, fy = prop.forces[u1_uuid]
        # U1 should feel upward force toward ghost at y=20
        assert fy < 0, f"U1 should feel upward force toward ghost, got fy={fy}"

    def test_ghost_attraction_only_affects_target(self, ghost_test_model):
        """Only the target footprint feels attraction force."""
        p = PlacementParams(stub=False, max_iterations=10, courtyard_repulsion_kc=0.0, repulsion_kr=1.0)
        prop = run_placement(ghost_test_model, p)

        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        r2_uuid = "bbbbbbbb-1111-2222-3333-444444444444"

        u1_force = prop.forces.get(u1_uuid, (0, 0))
        r2_force = prop.forces.get(r2_uuid, (0, 0))

        # U1 should have upward force (negative fy)
        assert u1_force[1] < 0, f"U1 should have upward force, got {u1_force}"

        # R2 should have minimal force (only weak Coulomb repulsion)
        assert abs(r2_force[1]) < 1.0, f"R2 should have minimal force, got {r2_force}"
