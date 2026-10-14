"""Tests for the placement domain: physics runners.

Test categories (pytest markers):
- unit: Pure logic, no fixtures
- physics: force-spring simulation (requires nets)
- minimal: Uses minimal.kicad_pcb fixture (4 footprints, no nets)
- dccf: Uses DCCF.sved.kicad_pcb fixture (97 footprints, 59 nets)
"""

import math
from pathlib import Path

import numpy as np
import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    Footprint,
    Pad,
    Point,
    apply_deltas,
    board_model,
)
from kicad_autorouter.placement import (
    PlacementParams,
    _collect_pad_nodes,
    _run_region_simulation,
    run_placement,
)
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp as make_test_fp

FIXTURES = Path(__file__).parent / "fixtures"
TUBE111_PCB = FIXTURES / "tube111.kicad_pcb"
GHOST_TEST_PCB = FIXTURES / "ghost_test.kicad_pcb"


@pytest.fixture(scope="module")
def ghost_test_model():
    return board_model(parse_file(GHOST_TEST_PCB))


@pytest.mark.physics
@pytest.mark.dccf
class TestPhysicsRunner:
    """Force-spring simulation with rigid projection - requires nets."""

    def test_physics_moves_unlocked(self, dccf_model):
        p = PlacementParams(max_iterations=10)
        prop = run_placement(dccf_model, p)
        assert prop.deltas
        for uuid, (dx, dy, da) in prop.deltas.items():
            assert isinstance(dx, float)
            assert isinstance(dy, float)
            assert isinstance(da, float)
        assert prop.iterations > 0
        assert prop.final_max_disp_mm > 0.0

    def test_physics_respects_locked(self, dccf_model):
        p = PlacementParams(max_iterations=10)
        prop = run_placement(dccf_model, p)
        for uuid in prop.deltas:
            fp = next(fp for fp in dccf_model.footprints if fp.uuid == uuid)
            assert not fp.locked

    def test_selective_movable_uuids(self, dccf_model):
        p = PlacementParams(max_iterations=10)
        movable_fp = next(fp for fp in dccf_model.footprints if not fp.locked and fp.uuid)
        prop = run_placement(dccf_model, p, movable_uuids={movable_fp.uuid})
        assert prop.deltas
        assert set(prop.deltas) == {movable_fp.uuid}

    def test_selective_movable_uuids_empty(self, dccf_model):
        p = PlacementParams(max_iterations=10)
        prop = run_placement(dccf_model, p, movable_uuids=set())
        assert prop.deltas == {}

    def test_selective_movable_uuids_excludes_locked(self, dccf_model):
        p = PlacementParams(max_iterations=10)
        locked_fps = [fp for fp in dccf_model.footprints if fp.locked]
        if not locked_fps:
            pytest.skip("No locked footprints in DCCF fixture")
        locked_fp = locked_fps[0]
        prop = run_placement(dccf_model, p, movable_uuids={locked_fp.uuid})
        assert prop.deltas == {}

    def test_convergence_stops_early(self, dccf_model):
        p = PlacementParams(max_iterations=1000, convergence_eps_mm=100.0)
        prop = run_placement(dccf_model, p)
        assert prop.iterations < 1000

    def test_projection_preserves_footprint_shape(self, dccf_model):
        """Rigid projection keeps every footprint's shape (within 1e-9)."""
        from kicad_autorouter.placement import _collect_pad_nodes

        pads, _, fp_uuid_to_pad_indices, _, _ = _collect_pad_nodes(dccf_model)
        before = np.array([[p.position.x_mm, p.position.y_mm] for p in pads])
        p = PlacementParams(max_iterations=10)
        prop = run_placement(dccf_model, p)
        moved = apply_deltas(dccf_model, prop.deltas)
        pads2, _, fp_uuid_to_pad_indices2, _, _ = _collect_pad_nodes(moved)
        after = np.array([[p.position.x_mm, p.position.y_mm] for p in pads2])
        assert prop.deltas, "short DCCF run must move something"
        for uuid, rows in fp_uuid_to_pad_indices.items():
            rows2 = fp_uuid_to_pad_indices2[uuid]
            assert len(rows) == len(rows2)
            if len(rows) < 2:
                continue
            d0 = np.linalg.norm(before[rows] - before[rows[0]], axis=1)
            d1 = np.linalg.norm(after[rows2] - after[rows2[0]], axis=1)
            assert d0 == pytest.approx(d1, rel=1e-9, abs=1e-12), (
                f"footprint {uuid} deformed"
            )


@pytest.mark.physics
@pytest.mark.dccf
class TestPerRegionSimulation:
    """Tests for multi-region placement simulation."""

    def test_per_region_simulation(self, dccf_model):
        """DCCF parts don't collapse to middle between boards."""
        p = PlacementParams(max_iterations=10)
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


@pytest.mark.physics
@pytest.mark.dccf
class TestCourtyardCollision:
    """Tests for courtyard polygon collision detection (shapely-based)."""

    def test_courtyard_polygon_collision(self, dccf_model):
        """Overlapping courtyard polygons should generate repulsion forces."""
        p = PlacementParams(max_iterations=5, courtyard_repulsion_kc=2000.0)
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


@pytest.mark.physics
@pytest.mark.minimal
class TestGhostComponents:
    """Tests for ghost/pseudo component behavior in placement."""

    def test_ghost_repels_real_footprints(self, ghost_test_model):
        """Ghost footprint with courtyard should repel real footprints via collision."""
        # Run placement with strong courtyard repulsion
        p = PlacementParams(
            max_iterations=50,
            courtyard_repulsion_kc=5000.0,
            # Ghost rail (~y=15) sits ~35mm from R1/R2: halo must reach it.
            courtyard_halo_mm=40.0,
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

    def test_ghost_courtyard_collision_force(self, ghost_test_model):
        """Ghost courtyard collision generates forces on real footprints."""
        p = PlacementParams(
            max_iterations=5,
            courtyard_repulsion_kc=2000.0,
            # ~35mm ghost-to-part gap: halo must reach it (bounded regime).
            courtyard_halo_mm=40.0,
        )
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
        target_ref, ka = ghost.ghost_attractions[0]
        assert target_ref == "U1"
        assert ka == 0.1

    def test_ghost_attracts_target_footprint(self, ghost_test_model):
        """Ghost with attraction pulls target footprint toward it."""
        # Ghost at y=20, U1 at y=50. Attraction should pull U1 up (negative dy).
        p = PlacementParams(
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
        p = PlacementParams(max_iterations=5, courtyard_repulsion_kc=0.0)
        prop = run_placement(ghost_test_model, p)

        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        assert u1_uuid in prop.forces
        fx, fy = prop.forces[u1_uuid]
        # U1 should feel upward force toward ghost at y=20
        assert fy < 0, f"U1 should feel upward force toward ghost, got fy={fy}"

    def test_ghost_attraction_only_affects_target(self, ghost_test_model):
        """Only the target footprint feels attraction force."""
        p = PlacementParams(max_iterations=10, courtyard_repulsion_kc=0.0, repulsion_kr=1.0)
        prop = run_placement(ghost_test_model, p)

        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        r2_uuid = "bbbbbbbb-1111-2222-3333-444444444444"

        u1_force = prop.forces.get(u1_uuid, (0, 0))
        r2_force = prop.forces.get(r2_uuid, (0, 0))

        # U1 should have upward force (negative fy)
        assert u1_force[1] < 0, f"U1 should have upward force, got {u1_force}"

        # R2 should have minimal force (only weak Coulomb repulsion)
        assert abs(r2_force[1]) < 1.0, f"R2 should have minimal force, got {r2_force}"


@pytest.mark.physics
class TestForceComponents:
    """Per-cause force breakdown for the tuning overlay."""

    def test_ghost_component_visible_on_target(self, ghost_test_model):
        p = PlacementParams(max_iterations=5, courtyard_repulsion_kc=0.0)
        prop = run_placement(ghost_test_model, p)
        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        comps = prop.force_components.get(u1_uuid, {})
        assert "ghost" in comps
        assert comps["ghost"][1] < 0, "ghost pull on U1 should point up"

    def test_forces_predict_next_step_not_residual(self):
        """Reported forces are evaluated at the committed board (F0).

        They must be horizon-independent (identical for a static probe and a
        multi-iteration run from the same pose) so the overlay predicts the
        next Step instead of showing the residual at the preview end.
        """
        m = _two_squares_model(gap_mm=2.0)
        base = dict(
            repulsion_kr=1.0,
            courtyard_repulsion_kc=100.0,
            courtyard_halo_mm=3.0,
        )
        static_prop = run_placement(m, PlacementParams(max_iterations=0, **base))
        stepping_prop = run_placement(m, PlacementParams(max_iterations=5, **base))
        assert static_prop.forces.keys() == stepping_prop.forces.keys()
        for uuid, f_static in static_prop.forces.items():
            f_step = stepping_prop.forces[uuid]
            assert f_step == pytest.approx(f_static)
            c_static = static_prop.force_components[uuid]
            c_step = stepping_prop.force_components[uuid]
            assert set(c_step) == set(c_static)
            for cause, v_static in c_static.items():
                assert c_step[cause] == pytest.approx(v_static)

    def test_single_step_delta_follows_reported_force(self):
        """For a 1-iteration run the displacement must head along F0."""
        m = _two_squares_model(gap_mm=2.0)
        p = PlacementParams(
            max_iterations=1, repulsion_kr=1.0,
            courtyard_repulsion_kc=100.0, courtyard_halo_mm=3.0,
        )
        prop = run_placement(m, p)
        for uuid, (dx, dy, _da) in prop.deltas.items():
            fx, fy = prop.forces[uuid]
            assert fx * dx + fy * dy > 0.0, (
                f"{uuid}: delta {(dx, dy)} opposes reported force {(fx, fy)}"
            )


def _two_squares_model(gap_mm: float) -> BoardModel:
    """Two 1-pad footprints with 2x2mm square courtyards, `gap_mm` apart edge-to-edge."""

    def make_fp(ref: str, uuid: str, net: str, x: float) -> Footprint:
        return make_test_fp(ref, uuid, x, 0.0, (net,),
                            footprint_id="T:R_0805", hw=1.0, hh=1.0,
                            pad_shape="circle", pad_layers=())

    fpa = make_fp("A1", "aaaaaaaa-0000-0000-0000-000000000001", "N1", 0.0)
    fpb = make_fp("B1", "bbbbbbbb-0000-0000-0000-000000000002", "N2", 2.0 + gap_mm)
    region = BoardRegion(uuid="r0", polygon=(), bbox=(-10.0, 20.0, -10.0, 10.0))
    return BoardModel(
        footprints=(fpa, fpb),
        keepout_zones=(),
        nets={},
        by_ref={"A1": fpa, "B1": fpb},
        board_regions=(region,),
        footprint_region={fpa.uuid: 0, fpb.uuid: 0},
    )


def _courtyard_of(prop, uuid: str):
    return prop.force_components[uuid]["courtyard"]


@pytest.mark.unit
class TestCourtyardHalo:
    """Bounded halo: only close approaches push; strength saturates at kc."""

    def test_inside_halo_pushes(self):
        m = _two_squares_model(gap_mm=2.0)
        p = PlacementParams(
            max_iterations=0, repulsion_kr=1.0,
            courtyard_repulsion_kc=100.0, courtyard_halo_mm=3.0,
        )
        prop = run_placement(m, p)
        fx, fy = _courtyard_of(prop, "aaaaaaaa-0000-0000-0000-000000000001")
        assert fx < 0, f"A1 should be pushed left, got {(fx, fy)}"
        assert abs(fx) > 0.0

    def test_outside_halo_silent(self):
        m = _two_squares_model(gap_mm=4.0)
        p = PlacementParams(
            max_iterations=0, repulsion_kr=1.0,
            courtyard_repulsion_kc=100.0, courtyard_halo_mm=3.0,
        )
        prop = run_placement(m, p)
        for uuid in (
            "aaaaaaaa-0000-0000-0000-000000000001",
            "bbbbbbbb-0000-0000-0000-000000000002",
        ):
            fx, fy = _courtyard_of(prop, uuid)
            assert (fx, fy) == pytest.approx((0.0, 0.0))

    def test_falloff_capped_and_monotonic(self):
        kc = 100.0
        mags = []
        for gap in (0.1, 1.5, 2.9):
            m = _two_squares_model(gap_mm=gap)
            p = PlacementParams(
                max_iterations=0, repulsion_kr=1.0,
                courtyard_repulsion_kc=kc, courtyard_halo_mm=3.0,
            )
            prop = run_placement(m, p)
            fx, fy = _courtyard_of(prop, "aaaaaaaa-0000-0000-0000-000000000001")
            mags.append(math.hypot(fx, fy))
        assert all(mag < kc for mag in mags), f"strength must saturate at kc: {mags}"
        assert mags[0] > mags[1] > mags[2] > 0.0, f"must fall with gap: {mags}"

    @pytest.mark.dccf
    def test_c11_close_neighbor_contact_is_bounded(self, dccf_model):
        """C11 feels bounded halo contact from its true close neighbor.

        Regression for the report: old size-scaled halo had RV sockets
        pushing C11 from 20-30mm away with ~3800 units.

        Re-baselined 2026-10-10 (was test_c11_quarter_inch_clearance_is_quiet):
        the courtyard-outline fix (polygonize/unary_union) recovered true
        outlines, and C11/R11 now measure 1.30mm apart (inside the 3mm halo;
        R14 sits exactly at the 3.00mm halo edge). So ~22.7 units of
        legitimate halo contact replaced the buggy-shape 0.0. The upper
        bound (< 50) still catches the old ~3800 blow-up by two orders of
        magnitude; the lower bound (> 5) pins that contact is detected
        (guards against a regression back to silent pad-bbox shapes).

        Measured at the rest layout (max_iterations=0) on purpose: after
        live iterations the pads slosh on mm scale (hot start + stiff
        contact) and the reported force oscillates 0 -> 18 -> 2.5 -> 17
        across adjacent iteration counts (2026-10-10), so any threshold
        on the post-motion value pins a chaotic landing, not physics.
        """
        p = PlacementParams(
            max_iterations=0,
            courtyard_repulsion_kc=20.0,
            courtyard_halo_mm=3.0,
        )
        prop = run_placement(
            dccf_model, p,
            movable_uuids={"883e9176-8fb8-4697-9a2d-1a656ba552fe"},
        )
        uuid = "883e9176-8fb8-4697-9a2d-1a656ba552fe"
        comps = prop.force_components[uuid]
        courtyard = math.hypot(*comps["courtyard"])
        # Legitimate R11 contact at 1.3mm gap measures ~22.7; was ~3800
        # from 20-30mm-away RV sockets under the old size-scaled halo,
        # and 0.0 under the buggy pad-bbox shapes.
        # With pin courtyards for THT pads, additional cross-layer collisions
        # are detected (e.g., RV1 THT pins vs C11 body), increasing force.
        assert 5.0 < courtyard < 100.0, f"C11 courtyard={courtyard:.2f}"