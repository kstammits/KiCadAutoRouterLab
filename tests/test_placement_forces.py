"""Force-matrix behavior suite: what each placement force does, in isolation.

Companion to tests/test_magnet.py (clustering heuristics): where the magnet
suite pins emergent multi-part behavior, this module pins each force's role
by zeroing it (or isolating it) on tiny two-part boards and asserting the
qualitative outcome. If a deliberate physics change moves these, update the
thresholds AND justify against the intent stated in each docstring — never
loosen a bound just to make red go green.

Conventions: boards mirror tests/test_placement_pairs.py (valid head-to-tail
courtyard rects at board coords, virtual single region from Edge.Cuts).
Probes use max_iterations=0 (static force at the committed pose); runs use
short budgets with convergence_eps_m=1e-15 so the full budget executes.
"""

import math
from dataclasses import replace

import pytest

from kicad_autorouter.board_model import BoardModel, Point, apply_deltas
from kicad_autorouter.placement import PlacementParams, run_placement

from .helpers import make_fp, overlap_area


EDGE = 30.0


def _board(fps):
    """Create a board model with a single region from edge cuts."""
    edge_cuts = (
        (Point(-EDGE, -EDGE), Point(EDGE, -EDGE)),
        (Point(EDGE, -EDGE), Point(EDGE, EDGE)),
        (Point(EDGE, EDGE), Point(-EDGE, EDGE)),
        (Point(-EDGE, EDGE), Point(-EDGE, -EDGE)),
    )
    # Create region polygon from edge cuts
    from shapely.geometry import Polygon as ShapelyPolygon
    from kicad_autorouter.board_model import BoardRegion
    region_poly = ShapelyPolygon([(-EDGE, -EDGE), (EDGE, -EDGE), (EDGE, EDGE), (-EDGE, EDGE)])
    board_regions = (
        BoardRegion(
            uuid="region-0",
            polygon=tuple(Point(x, y) for x, y in region_poly.exterior.coords),
            bbox=(-EDGE, EDGE, -EDGE, EDGE),
        ),
    )
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=edge_cuts,
        footprint_region={fp.uuid: 0 for fp in fps},
        board_regions=board_regions,
    )


def _single_pad(ref, uuid, x, y, net, hw=1.0, hh=1.0):
    return make_fp(ref, uuid, x, y, (net,),
                   footprint_id="T:R_0805", hw=hw, hh=hh,
                   pad_shape="circle", pad_layers=())


def _overlap(fa_uuid="uuid-a", fb_uuid="uuid-b", dx=1.0, dy=0.0):
    """Two 6x4mm parts (dx, dy) apart (heavy courtyard overlap), distinct nets."""
    fa = make_fp("A", fa_uuid, 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", fb_uuid, dx, dy, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    return _board([fa, fb])


def _base_params(**kw):
    base = dict(
        max_iterations=10,
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05,
        wall_k_N=0.01,
        wall_halo_m=0.003,
        wall_exponent=1.0,
        momentum_alpha=0.0,
    )
    base.update(kw)
    return PlacementParams(**base)


def _courtyard_mag(prop, uuid):
    fx, fy = prop.force_components[uuid]["courtyard"]
    return math.hypot(fx, fy)


@pytest.mark.physics
class TestOverlapLaw:
    """The courtyard overlap push must grow with penetration, not shrink.

    Regression for the kc/dist form (weaker when deeper, silent when
    coincident) that let stacked parts pile up: with constant-magnitude
    net attraction pulling parts together, overlap is the only thing
    stopping collapse, so its force must be strongest where overlap is
    deepest.
    """

    def test_deeper_overlap_pushes_harder(self):
        # Unified wall law F = k*(1+pen/h): pen is the SAT depth along the
        # MTV axis, so deepening must happen ALONG that axis. Fixed x-offset
        # holds x-overlap at 4mm while y-overlap grows (depths 2.5/3/3.5mm);
        # sliding along x past the y-overlap would (correctly) read flat.
        mags = []
        for dy in (1.5, 1.0, 0.5):
            model = _overlap(dx=2.0, dy=dy)
            prop = run_placement(model, _base_params(max_iterations=0))
            mags.append(_courtyard_mag(prop, "uuid-b"))
        assert mags[0] > 0.0, "even shallow overlap must push"
        assert mags[0] < mags[1] < mags[2], (
            f"push must grow with penetration: {mags}"
        )

    def test_contact_is_continuous(self):
        """First touch from outside (halo max = kc) meets first touch from
        inside (kc * (1 + ~0 area)): no force cliff at the contact plane
        that the integrator could tunnel through."""
        # 2x2 squares touching edge-to-edge: halo branch at zero gap.
        touching = _board([
            _single_pad("A", "uuid-a", 0.0, 0.0, "N1"),
            _single_pad("B", "uuid-b", 2.0, 0.0, "N2"),
        ])
        prop_out = run_placement(touching, _base_params(max_iterations=0))
        # 0.01mm inside: overlap branch on a sliver of area.
        overlapping = _board([
            _single_pad("A", "uuid-a", 0.0, 0.0, "N1"),
            _single_pad("B", "uuid-b", 1.99, 0.0, "N2"),
        ])
        prop_in = run_placement(overlapping, _base_params(max_iterations=0))
        halo_at_contact = _courtyard_mag(prop_out, "uuid-b")
        overlap_just_inside = _courtyard_mag(prop_in, "uuid-b")
        # SI newtons: halo saturates at wall_k_N (see _base_params).
        kc = 0.01
        assert halo_at_contact == pytest.approx(kc, rel=0.01)
        assert overlap_just_inside == pytest.approx(kc, rel=0.05)

    def test_coincident_parts_separate(self):
        """Perfectly stacked outlines previously felt zero force (the
        intersection centroid sat exactly on the polygon centroid and
        failed the dist guard). The pair must split in opposite
        directions.

        NOTE (SI MTV contact): separation escapes along the minimum-
        translation axis — the short side (y for these 6x4 rects), not
        the old centroid-tiebreak x. The axis is assert-agnostic; what
        matters is opposite nonzero motion plus shrinking overlap.
        """
        model = _overlap(dx=0.0)
        before = overlap_area(model, {})
        assert before > 20.0
        prop = run_placement(model, _base_params(max_iterations=3))
        after = overlap_area(model, prop.deltas)
        assert after < before, f"coincident parts must separate ({before} -> {after})"
        dx_a, dy_a, _ = prop.deltas["uuid-a"]
        dx_b, dy_b, _ = prop.deltas["uuid-b"]
        dot = dx_a * dx_b + dy_a * dy_b
        assert dot < 0.0, (
            f"pair must move in opposite directions ({dx_a, dy_a} vs {dx_b, dy_b})"
        )
        assert math.hypot(dx_a, dy_a) > 1e-9, "separation must be nonzero"

    def test_overlap_resolves_to_zero(self):
        # NOTE (SI damping): the damped approach needs ~30 iterations to
        # clear a 20mm² overlap with wall_k_N=0.01; the intent (no
        # persistent interpenetration) is unchanged, only the runway.
        model = _overlap(dx=1.0)
        prop = run_placement(model, _base_params(max_iterations=30))
        assert overlap_area(model, prop.deltas) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.physics
class TestAttractionRepulsionEquilibrium:
    """Global attraction vs global repulsion must settle at a finite gap.

    With constant-magnitude net attraction (ka, always pulling) and Coulomb
    pad repulsion (kr/d^2, always pushing), one pad pair balances at
    d = sqrt(kr/ka). This is the equilibrium the ideal_length removal was
    meant to leave behind: assert it directly, without courtyards or walls
    in the way.
    """

    def test_pair_settles_at_sqrt_kr_over_ka(self):
        # SI units: kr in N·m², ka in N, equilibrium d = sqrt(kr/ka) meters.
        kr, ka = 5e-7, 5e-3  # ~10mm equilibrium
        model = _board([
            _single_pad("A", "uuid-a", 0.0, 0.0, "N1", hw=0.25, hh=0.25),
            _single_pad("B", "uuid-b", 8.0, 0.0, "N1", hw=0.25, hh=0.25),
        ])
        prop = run_placement(
            model,
            _base_params(max_iterations=500,
                         convergence_eps_m=1e-09,
                         repulsion_k_N_m2=kr, attraction_k_N=ka,
                         wall_k_N=1e-15,
                         ),
        )
        moved = apply_deltas(model, prop.deltas)
        ax = moved.by_ref["A"].x_mm
        bx = moved.by_ref["B"].x_mm
        assert abs(bx - ax) == pytest.approx(
            math.sqrt(kr / ka) * 1000.0, rel=0.25)

    def test_attraction_pulls_together(self):
        model = _board([
            _single_pad("A", "uuid-a", 0.0, 0.0, "N1"),
            _single_pad("B", "uuid-b", 10.0, 0.0, "N1"),
        ])
        prop = run_placement(
            model,
            _base_params(max_iterations=5, repulsion_k_N_m2=1e-18,
                         attraction_k_N=0.001, wall_k_N=1e-15,
                         ),
        )
        moved = apply_deltas(model, prop.deltas)
        assert moved.by_ref["B"].x_mm < 10.0
        assert moved.by_ref["A"].x_mm > 0.0

    def test_no_attraction_parts_drift_apart(self):
        model = _board([
            _single_pad("A", "uuid-a", 0.0, 0.0, "N1"),
            _single_pad("B", "uuid-b", 10.0, 0.0, "N2"),
        ])
        prop = run_placement(
            model,
            _base_params(max_iterations=5, attraction_k_N=0.0,
                         wall_k_N=1e-15,
                         ),
        )
        moved = apply_deltas(model, prop.deltas)
        assert moved.by_ref["B"].x_mm > 10.0
        assert moved.by_ref["A"].x_mm < 0.0


@pytest.mark.physics
class TestCourtyardRole:
    """Courtyard collision is what separates bodies; pad Coulomb alone is
    too weak at body scale (and singular at pad scale)."""

    def test_without_courtyard_force_eject_still_clears(self):
        # Final geometric eject (not wall force) shrinks the overlap: the
        # courtyard *force* stays negligible, proving weak pad Coulomb
        # alone still cannot push bodies apart — separation comes from the
        # post-dynamics de-penetration round (one relaxed round per run;
        # repeated legalize runs converge it the rest of the way).
        model = _overlap(dx=1.0)
        before = overlap_area(model, {})
        prop = run_placement(
            model,
            _base_params(max_iterations=5, repulsion_k_N_m2=1.0000000000000001e-11,
                         wall_k_N=1e-15),
        )
        after = overlap_area(model, prop.deltas)
        assert after < before, (
            f"eject round must shrink overlap even with negligible wall force ({before} -> {after})"
        )
        assert _courtyard_mag(prop, "uuid-b") < 1e-9, (
            "wall force must stay negligible: Coulomb alone still cannot push"
        )

    def test_halo_reaches_before_contact(self):
        """Parts inside the halo but not touching feel a gentle push that
        grows as the gap closes (bounded, saturating at kc)."""
        mags = []
        for gap in (2.9, 1.5, 0.1):
            model = _board([
                _single_pad("A", "uuid-a", 0.0, 0.0, "N1"),
                _single_pad("B", "uuid-b", 2.0 + gap, 0.0, "N2"),
            ])
            prop = run_placement(model, _base_params(max_iterations=0))
            mags.append(_courtyard_mag(prop, "uuid-b"))
        kc = 10000.0
        assert all(m < kc for m in mags)
        assert mags[0] < mags[1] < mags[2]


@pytest.mark.physics
class TestBoundaryRole:
    """Boundary repulsion is what keeps parts inside the region outline."""

    def test_boundary_holds_against_shove(self):
        # NOTE (SI unified): boundary is unified with courtyard via wall_k_N.
        # Halo is 6mm, so part at x=-29 (1mm from edge at -30) is well within halo.
        def run(kw):
            model = _board([
                _single_pad("A", "uuid-a", -29.0, 0.0, "N1",
                            hw=0.25, hh=0.25),
                _single_pad("B", "uuid-b", -18.0, 0.0, "N2",
                            hw=0.25, hh=0.25),
            ])
            return run_placement(
                model,
                _base_params(max_iterations=10, **kw),
            ), model

        # Unified system: boundary uses wall_k_N. Use 0.05N wall to hold against ~1.5mN shove.
        (walled, model) = run(dict(wall_k_N=0.05, wall_halo_m=0.006))
        (open_, _) = run(dict(wall_k_N=1e-9, wall_halo_m=0.006))
        ax_walled = apply_deltas(model, walled.deltas).by_ref["A"].x_mm
        ax_open = apply_deltas(model, open_.deltas).by_ref["A"].x_mm
        assert ax_walled >= -EDGE - 1.0, f"wall must hold part in: x={ax_walled}"
        assert ax_walled > ax_open, (
            f"wall must resist the shove ({ax_walled} vs {ax_open})"
        )


@pytest.mark.physics
class TestGhostRole:
    """Ghost attraction pulls its target; without it the target drifts."""

    def _ghost_board(self):
        target = _single_pad("T", "uuid-t", 0.0, 20.0, "N1")
        ghost = replace(
            _single_pad("G", "ghost-1", 0.0, 10.0, "NG",
                        hw=2.0, hh=2.0),
            ghost=True,
            ghost_attractions=(("T", 0.5),),
        )
        model = _board([target])
        model = replace(
            model,
            ghost_footprints=(ghost,),
            by_ref={**model.by_ref, "G": ghost},
            # Ghost attractions resolve only for region-mapped uuids.
            footprint_region={**model.footprint_region, "ghost-1": 0},
        )
        return model

    def test_ghost_pulls_target(self):
        model = self._ghost_board()
        prop = run_placement(
            model, _base_params(max_iterations=30, repulsion_k_N_m2=1e-09)
        )
        assert "uuid-t" in prop.deltas
        _, dy, _ = prop.deltas["uuid-t"]
        assert dy < 0.0, f"target must move toward ghost (up), got dy={dy}"
        fx, fy = prop.forces["uuid-t"]
        assert fy < 0.0
        assert prop.force_components["uuid-t"]["ghost"][1] < 0.0

    def test_ghost_never_moves(self):
        model = self._ghost_board()
        prop = run_placement(model, _base_params(max_iterations=30))
        assert "ghost-1" not in prop.deltas


@pytest.mark.physics
class TestRigidProjection:
    """Rigid projection keeps each footprint's shape exact while soft forces
    move it; there is no stiffness parameter anymore.

    Replaces TestRigidRole (2026-10-11): the old Hooke spring (k_eff = 5e4
    against O(0.1) Coulomb forces, integrated with a fixed 2mm step cap)
    was ~1e5x past the explicit-Euler stability limit and rang as a
    sustained period-2 slam — an isolated 2-pad pair froze at iter 2 then
    flip-flopped. Position-based projection (Kabsch snap per footprint per
    iteration) is unconditionally stable and exact to float precision."""

    def _shear_board(self):
        mover = make_fp("M", "uuid-m", 0.0, 0.0, ("N1", "N2"),
                        footprint_id="test:R", hw=1.5, hh=1.0,
                        pad_layers=("F.Cu",), pad_shape="circle")
        pusher = make_fp("P", "uuid-p", 2.5, 0.0, ("N9",),
                         footprint_id="test:R", hw=1.5, hh=1.0,
                         pad_layers=("F.Cu",), pad_shape="circle",
                         locked=True)
        return _board([mover, pusher])

    def _pair_span(self, model, deltas, uuid):
        from kicad_autorouter.placement import _collect_pad_nodes
        import numpy as np
        moved = apply_deltas(model, deltas)
        pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
        pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
        rows = uuid_to_idx[uuid]
        pts = pos[rows]
        return float(np.linalg.norm(pts[0] - pts[-1]))

    def test_projected_body_holds_shape_exactly(self):
        model = self._shear_board()
        before = self._pair_span(model, {}, "uuid-m")
        prop = run_placement(model, _base_params(max_iterations=30))
        assert "uuid-m" in prop.deltas, "shear must move the body"
        after = self._pair_span(model, prop.deltas, "uuid-m")
        # In-sim snap is float-exact; the apply_deltas writeback
        # round-trips through an angle, so allow 1ulp-level drift.
        assert after == pytest.approx(before, rel=1e-9, abs=1e-12), (
            f"projection must preserve shape ({before} vs {after})"
        )

    def test_pose_from_pads_roundtrips_nonzero_angle(self):
        """Kabsch angle must invert ``_to_board``'s ``R(-angle)``.

        Regression: returning ``atan2(R)`` directly made ``apply_deltas``
        rotate a part opposite to its integrated pads (2B Q1 appeared to
        swing into C6). A +20 deg board-space pad rotation must produce
        ``da = -20`` and reproduce the rotated pads exactly.
        """
        import numpy as np

        from kicad_autorouter.placement import _collect_pad_nodes
        from kicad_autorouter.placement.rigid import (
            _compute_footprint_pose_from_pads,
        )

        fp = make_fp("U1", "uuid-u1", 5.0, -2.0, ("N1", "N2", "N3"),
                     footprint_id="test:R", hw=1.0, hh=0.6,
                     pad_layers=("F.Cu",), pad_shape="circle")
        fp = replace(fp, angle_deg=35.0)
        model = _board([fp])
        pads, _, uuid_to_idx, global_to_local, _ = _collect_pad_nodes(model)
        pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
        rows = uuid_to_idx[fp.uuid]
        center = pos[rows].mean(axis=0)
        theta = math.radians(20.0)
        rot = np.array([[math.cos(theta), -math.sin(theta)],
                        [math.sin(theta), math.cos(theta)]])
        rotated = (pos[rows] - center) @ rot.T + center

        x_mm, y_mm, angle_deg = _compute_footprint_pose_from_pads(
            fp, rotated, rows, global_to_local
        )
        delta = (x_mm - fp.x_mm, y_mm - fp.y_mm, angle_deg - fp.angle_deg)

        assert delta[2] == pytest.approx(-20.0, abs=1e-6)
        moved = apply_deltas(model, {fp.uuid: delta})
        moved_pads, _, moved_idx, _, _ = _collect_pad_nodes(moved)
        got = np.array([(p.position.x_mm, p.position.y_mm) for p in moved_pads])
        assert np.allclose(got[moved_idx[fp.uuid]], rotated, atol=1e-9)

    def test_isolated_pair_drifts_monotonically(self):
        """Regression for the rigid-snap reversal: an isolated 2-pad pair
        under repulsion-only drift must move monotonically apart — no
        iter-2 freeze, no flip-flop from iter 3."""
        u1 = make_fp("U1", "uuid-u1", 0.0, 0.0, ("VCC", "GND"),
                     hw=0.25, hh=0.25)
        c1 = make_fp("C1", "uuid-c1", 30.0, 0.0, ("VCC", "GND"),
                     hw=0.25, hh=0.25)
        model = _board([u1, c1])

        def _dx(iters):
            params = _base_params(
                max_iterations=iters,
                repulsion_k_N_m2=1.0000000000000001e-07,
                attraction_k_N=5e-05,
                wall_k_N=1e-15,
                
                include_power_nets=False,
                decap_boost=0.0,
            )
            return run_placement(model, params).deltas.get("uuid-c1", (0.0, 0.0, 0.0))[0]

        traj = [_dx(n) for n in (1, 2, 3, 5, 10)]
        assert all(b > a > 0.0 for a, b in zip(traj, traj[1:])), (
            f"pair must drift monotonically apart, got {traj}"
        )

    def test_rotation_snap_halved_deadband_off(self):
        """The rigid snap applies half the measured rotation step and no
        deadband drops the tail: parking micro-rotations wedges pairs that
        otherwise work apart (magnet R6/R7), while idle burn is bounded by
        the stall rule instead. Translation is preserved in all cases.
        """
        import numpy as np

        from kicad_autorouter.placement import (
            MIN_ROT_STEP_RAD,
            _clamp_rigid_step,
        )

        assert MIN_ROT_STEP_RAD == pytest.approx(0.0)
        old = np.array([[0.0, 0.0], [2.0, 0.0]])

        def posed(deg):
            r = math.radians(deg)
            c, s = math.cos(r), math.sin(r)
            cc = old.mean(axis=0)
            return (old - cc) @ np.array([[c, s], [-s, c]]) + cc + [0.05, 0.0]

        def out_angle(deg):
            pos = posed(deg)
            _clamp_rigid_step(
                pos, old, [(np.array([0, 1]), old.copy())],
                np.array([True, True]),
                cap_mm=1.0, max_rot_rad=math.radians(5.0))
            va, vb = old[1] - old[0], pos[1] - pos[0]
            return abs(math.degrees(
                math.atan2(vb[1], vb[0]) - math.atan2(va[1], va[0])))

        assert out_angle(0.05) == pytest.approx(0.025, rel=0.01)
        assert out_angle(0.5) == pytest.approx(0.25, rel=0.01)
        assert out_angle(10.0) == pytest.approx(5.0, rel=0.01)

    def test_no_stiffness_knob(self):
        """The spring is gone: no rigid_stiffness field, and legacy files
        carrying it still load (from_dict ignores unknown keys)."""
        assert not hasattr(PlacementParams(), "rigid_stiffness")
        q = PlacementParams.from_dict(
            {"repulsion_k_N_m2": 1e-7, "rigid_stiffness": 5e5,
             "repulsion_kr": 4.0})
        assert q.repulsion_k_N_m2 == pytest.approx(1e-7)
        assert not hasattr(q, "repulsion_kr")
