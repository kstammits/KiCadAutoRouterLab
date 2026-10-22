"""Tests for the placement domain: physics runners.

Test categories (pytest markers):
- unit: Pure logic, no fixtures
- physics: force-spring simulation (requires nets)
- minimal: Uses minimal.kicad_pcb fixture (4 footprints, no nets)
- dccf: Uses DCCF.sved.kicad_pcb fixture (97 footprints, 59 nets)
"""

import math
from dataclasses import replace
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

    def test_coarse_eps_does_not_stop_under_load(self, dccf_model):
        """A coarse displacement epsilon must not declare rest while large
        pushes remain (displacement-only stopping can halt legalize with
        overlaps and ~0.1N residuals intact). Honest stop: budget or stalled
        with residual attached."""
        p = PlacementParams(max_iterations=1000, convergence_eps_m=0.1)
        prop = run_placement(dccf_model, p)
        assert prop.stop_reason in ("budget", "stalled")
        assert prop.residual_max_force_N > 1e-3

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
        p = PlacementParams(max_iterations=5, wall_k_N=0.002)
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
            wall_k_N=0.005,
            # Ghost rail (~y=15) sits ~35mm from R1/R2: halo must reach it.
            wall_halo_m=0.04,
            repulsion_k_N_m2=1e-08,  # Low Coulomb repulsion to isolate courtyard effect
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
            wall_k_N=0.002,
            # ~35mm ghost-to-part gap: halo must reach it (bounded regime).
            wall_halo_m=0.04,
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
            wall_k_N=0.0,  # Disable courtyard to isolate attraction
            repulsion_k_N_m2=1e-08,  # Low Coulomb repulsion
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
        p = PlacementParams(max_iterations=5, wall_k_N=0.0)
        prop = run_placement(ghost_test_model, p)

        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        assert u1_uuid in prop.forces
        fx, fy = prop.forces[u1_uuid]
        # U1 should feel upward force toward ghost at y=20
        assert fy < 0, f"U1 should feel upward force toward ghost, got fy={fy}"

    def test_ghost_attraction_only_affects_target(self, ghost_test_model):
        """Only the target footprint feels attraction force."""
        p = PlacementParams(max_iterations=10, wall_k_N=0.0, repulsion_k_N_m2=1e-09)
        prop = run_placement(ghost_test_model, p)

        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        r2_uuid = "bbbbbbbb-1111-2222-3333-444444444444"

        u1_force = prop.forces.get(u1_uuid, (0, 0))
        r2_force = prop.forces.get(r2_uuid, (0, 0))

        # U1 should have upward force (negative fy)
        assert u1_force[1] < 0, f"U1 should have upward force, got {u1_force}"

        # R2 should have minimal force (only weak Coulomb repulsion)
        assert abs(r2_force[1]) < 1.0, f"R2 should have minimal force, got {r2_force}"

    def test_straddling_ghost_wall_joins_region_and_pushes(self):
        """A ghost wall straddling the board edge still collides.

        Region membership is centroid-first, but a wall guarding an edge
        naturally sits half-outside: centroid outside, courtyard crossing
        into the board. Such a ghost must join the overlapped region (not
        -1, which excludes it from every region's simulation and silences
        it). Regression for a Q1 legalize case, where a 1.8mm overlap with a
        straddling wall measured exactly 0.0N.
        """
        from kicad_autorouter.board_model import _assign_board_regions
        from kicad_autorouter.ghost import make_ghost_wall

        edge = 30.0
        ghost = make_ghost_wall("GW", -31.0, 0.0, 4.0, 20.0, uuid="uuid-gw")
        # Courtyard x -33..-29 (centroid x=-31: outside the -30 edge),
        # crossing 1mm into the board.
        part = make_test_fp("A", "uuid-a", -27.5, 0.0, ("N1", "N2"),
                            hw=1.5, hh=1.0, pad_layers=("F.Cu",))
        # Courtyard x -29..-26: overlaps the ghost by 0.5mm.
        regions = (
            BoardRegion(
                uuid="region-0",
                polygon=(Point(-edge, -edge), Point(edge, -edge),
                         Point(edge, edge), Point(-edge, edge)),
                bbox=(-edge, edge, -edge, edge),
            ),
        )
        polys = [[Point(-edge, -edge), Point(edge, -edge),
                  Point(edge, edge), Point(-edge, edge)]]
        mapping = _assign_board_regions((part, ghost), polys)
        assert mapping["uuid-a"] == 0
        assert mapping["uuid-gw"] == 0, (
            "straddling ghost wall must join the overlapped region, not -1"
        )
        edge_cuts = (
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        )
        model = BoardModel(
            footprints=(part,),
            keepout_zones=(),
            nets={},
            by_ref={"A": part},
            edge_cuts=edge_cuts,
            ghost_footprints=(ghost,),
            footprint_region={part.uuid: 0, ghost.uuid: 0},
            board_regions=regions,
        )
        wall_k = 0.09
        prop = run_placement(
            model,
            PlacementParams(max_iterations=0, wall_k_N=wall_k,
                            wall_halo_m=0.006),
        )
        fx, fy = prop.force_components["uuid-a"]["courtyard"]
        # Ghost contact alone pushes east; the board edge (1mm away) adds
        # its own halo push east. Either way the sum must exceed
        # the board-only control below by the ghost's share.
        assert fx > 0.0 and abs(fy) < 1e-6
        bare = BoardModel(
            footprints=(part,),
            keepout_zones=(),
            nets={},
            by_ref={"A": part},
            edge_cuts=edge_cuts,
            footprint_region={part.uuid: 0},
            board_regions=regions,
        )
        prop_bare = run_placement(
            bare,
            PlacementParams(max_iterations=0, wall_k_N=wall_k,
                            wall_halo_m=0.006),
        )
        fx_bare, _ = prop_bare.force_components["uuid-a"]["courtyard"]
        assert fx - fx_bare == pytest.approx(wall_k * (1.0 + 0.5 / 6.0),
                                             rel=0.1), (
            f"ghost wall must contribute its contact push, got {fx} vs {fx_bare}"
        )


@pytest.mark.physics
class TestForceComponents:
    """Per-cause force breakdown for the tuning overlay."""

    def test_ghost_component_visible_on_target(self, ghost_test_model):
        p = PlacementParams(max_iterations=5, wall_k_N=0.0)
        prop = run_placement(ghost_test_model, p)
        u1_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        comps = prop.force_components.get(u1_uuid, {})
        assert "ghost" in comps
        assert comps["ghost"][1] < 0, "ghost pull on U1 should point up"

    def test_forces_predict_next_step_not_residual(self):
        """Reported forces are evaluated at the final committed position.

        A static force probe rebuilt at the final positions of a stepping run
        must reproduce the stepping run's reported forces, so the overlay
        matches where parts actually ended up (not the stale F0 pose).
        """
        m = _two_squares_model(gap_mm=2.0)
        base = dict(
            repulsion_k_N_m2=1e-09,
            wall_k_N=9.999999999999999e-05,
            wall_halo_m=0.003,
        )
        stepping = run_placement(m, PlacementParams(max_iterations=5, **base))

        shifted = []
        for fp in m.footprints:
            dx, dy, da = stepping.deltas.get(fp.uuid, (0.0, 0.0, 0.0))
            shifted.append(
                replace(fp, x_mm=fp.x_mm + dx, y_mm=fp.y_mm + dy,
                        angle_deg=fp.angle_deg + da)
            )
        at_end = BoardModel(
            footprints=tuple(shifted),
            keepout_zones=(),
            nets={},
            by_ref={fp.ref: fp for fp in shifted},
            board_regions=m.board_regions,
            footprint_region={fp.uuid: 0 for fp in shifted},
        )
        static_at_end = run_placement(at_end, PlacementParams(max_iterations=0, **base))

        assert stepping.forces.keys() == static_at_end.forces.keys()
        for uuid, f_step in stepping.forces.items():
            f_end = static_at_end.forces[uuid]
            assert f_end == pytest.approx(f_step, rel=1e-3)
            c_step = stepping.force_components[uuid]
            c_end = static_at_end.force_components[uuid]
            assert set(c_end) == set(c_step)
            for cause, v_step in c_step.items():
                if cause == "tangential":
                    continue  # tangential is velocity-dependent; static probe has no velocity history
                assert c_end[cause] == pytest.approx(v_step, rel=1e-3, abs=1e-9)

    def test_single_step_delta_follows_reported_force(self):
        """For a 1-iteration run the displacement must head along F0."""
        m = _two_squares_model(gap_mm=2.0)
        p = PlacementParams(
            max_iterations=1, repulsion_k_N_m2=1e-09,
            wall_k_N=9.999999999999999e-05, wall_halo_m=0.003,
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
            max_iterations=0, repulsion_k_N_m2=1e-09,
            wall_k_N=9.999999999999999e-05, wall_halo_m=0.003,
        )
        prop = run_placement(m, p)
        fx, fy = _courtyard_of(prop, "aaaaaaaa-0000-0000-0000-000000000001")
        assert fx < 0, f"A1 should be pushed left, got {(fx, fy)}"
        assert abs(fx) > 0.0

    def test_outside_halo_silent(self):
        m = _two_squares_model(gap_mm=4.0)
        p = PlacementParams(
            max_iterations=0, repulsion_k_N_m2=1e-09,
            wall_k_N=9.999999999999999e-05, wall_halo_m=0.003,
        )
        prop = run_placement(m, p)
        for uuid in (
            "aaaaaaaa-0000-0000-0000-000000000001",
            "bbbbbbbb-0000-0000-0000-000000000002",
        ):
            fx, fy = _courtyard_of(prop, uuid)
            assert (fx, fy) == pytest.approx((0.0, 0.0))

    def test_falloff_capped_and_monotonic(self):
        kc = 0.01  # SI newtons: halo saturates at wall_k_N
        mags = []
        for gap in (0.1, 1.5, 2.9):
            m = _two_squares_model(gap_mm=gap)
            p = PlacementParams(
                max_iterations=0, repulsion_k_N_m2=1e-09,
                wall_k_N=kc, wall_halo_m=0.003,
            )
            prop = run_placement(m, p)
            fx, fy = _courtyard_of(prop, "aaaaaaaa-0000-0000-0000-000000000001")
            mags.append(math.hypot(fx, fy))
        assert all(mag < kc for mag in mags), f"strength must saturate at kc: {mags}"
        assert mags[0] > mags[1] > mags[2] > 0.0, f"must fall with gap: {mags}"

    @pytest.mark.dccf
    def test_c11_close_neighbor_contact_is_bounded(self, dccf_model):
        """C11 feels bounded halo contact from its true close neighbor.

        True courtyards put C11/R11 1.30mm apart (inside the 3mm halo; R14
        sits exactly at the 3.00mm halo edge), so the halo reads ~0.57×wall.
        The upper bound (1.0×wall) catches far-field blow-ups; the lower
        bound (0.1×wall) pins that contact is detected (silent pad-bbox
        shapes read exactly 0.0).

        Measured at the rest layout (max_iterations=0) on purpose: after
        live iterations the pads slosh on mm scale (hot start + stiff
        contact) and the reported force oscillates across adjacent iteration
        counts, so a threshold on the post-motion value pins a chaotic
        landing, not physics.
        """
        wall = 1.9999999999999998e-05
        p = PlacementParams(
            max_iterations=0,
            wall_k_N=wall,
            wall_halo_m=0.003,
        )
        prop = run_placement(
            dccf_model, p,
            movable_uuids={"883e9176-8fb8-4697-9a2d-1a656ba552fe"},
        )
        uuid = "883e9176-8fb8-4697-9a2d-1a656ba552fe"
        comps = prop.force_components[uuid]
        courtyard = math.hypot(*comps["courtyard"])
        # With pin courtyards for THT pads, additional cross-layer collisions
        # are detected (e.g., RV1 THT pins vs C11 body), increasing force.
        assert 0.1 * wall < courtyard < 1.0 * wall, f"C11 courtyard={courtyard:.2g}"