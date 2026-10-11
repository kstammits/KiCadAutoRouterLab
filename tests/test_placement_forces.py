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
short budgets with convergence_eps_mm=1e-12 so the full budget executes.
"""

import math
from dataclasses import replace

import pytest

from kicad_autorouter.board_model import BoardModel, Point, apply_deltas
from kicad_autorouter.placement import PlacementParams, run_placement

from .helpers import make_fp, overlap_area


EDGE = 30.0


def _board(fps):
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-EDGE, -EDGE), Point(EDGE, -EDGE)),
            (Point(EDGE, -EDGE), Point(EDGE, EDGE)),
            (Point(EDGE, EDGE), Point(-EDGE, EDGE)),
            (Point(-EDGE, EDGE), Point(-EDGE, -EDGE)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def _single_pad(ref, uuid, x, y, net, hw=1.0, hh=1.0):
    return make_fp(ref, uuid, x, y, (net,),
                   footprint_id="T:R_0805", hw=hw, hh=hh,
                   pad_shape="circle", pad_layers=())


def _overlap(fa_uuid="uuid-a", fb_uuid="uuid-b", dx=1.0):
    """Two 6x4mm parts dx apart (heavy courtyard overlap), distinct nets."""
    fa = make_fp("A", fa_uuid, 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", fb_uuid, dx, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    return _board([fa, fb])


def _base_params(**kw):
    base = dict(
        max_iterations=10,
        convergence_eps_mm=1e-12,
        repulsion_kr=100.0,
        attraction_ka=0.05,
        courtyard_repulsion_kc=10000.0,
        courtyard_halo_mm=3.0,
        boundary_repulsion_kb=1.0,
        rigid_stiffness=5e5,
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
        mags = []
        for dx in (2.0, 1.0, 0.5):
            model = _overlap(dx=dx)
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
        kc = 10000.0
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
        assert halo_at_contact == pytest.approx(kc, rel=0.01)
        assert overlap_just_inside == pytest.approx(kc, rel=0.05)

    def test_coincident_parts_separate(self):
        """Perfectly stacked outlines previously felt zero force (the
        intersection centroid sat exactly on the polygon centroid and
        failed the dist guard). The deterministic tiebreak must split
        them in opposite directions."""
        model = _overlap(dx=0.0)
        before = overlap_area(model, {})
        assert before > 20.0
        prop = run_placement(model, _base_params(max_iterations=3))
        after = overlap_area(model, prop.deltas)
        assert after < before, f"coincident parts must separate ({before} -> {after})"
        dx_a, _, _ = prop.deltas["uuid-a"]
        dx_b, _, _ = prop.deltas["uuid-b"]
        assert dx_a * dx_b < 0.0, (
            f"pair must split in opposite x directions ({dx_a} vs {dx_b})"
        )

    def test_overlap_resolves_to_zero(self):
        model = _overlap(dx=1.0)
        prop = run_placement(model, _base_params(max_iterations=10))
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
        kr, ka = 4.0, 1.0
        model = _board([
            _single_pad("A", "uuid-a", 0.0, 0.0, "N1", hw=0.25, hh=0.25),
            _single_pad("B", "uuid-b", 8.0, 0.0, "N1", hw=0.25, hh=0.25),
        ])
        prop = run_placement(
            model,
            _base_params(max_iterations=500,
                         convergence_eps_mm=1e-6,
                         repulsion_kr=kr, attraction_ka=ka,
                         courtyard_repulsion_kc=1e-9,
                         boundary_repulsion_kb=1e-9),
        )
        moved = apply_deltas(model, prop.deltas)
        ax = moved.by_ref["A"].x_mm
        bx = moved.by_ref["B"].x_mm
        assert abs(bx - ax) == pytest.approx(math.sqrt(kr / ka), rel=0.25)

    def test_attraction_pulls_together(self):
        model = _board([
            _single_pad("A", "uuid-a", 0.0, 0.0, "N1"),
            _single_pad("B", "uuid-b", 10.0, 0.0, "N1"),
        ])
        prop = run_placement(
            model,
            _base_params(max_iterations=5, repulsion_kr=1e-9,
                         attraction_ka=1.0, courtyard_repulsion_kc=1e-9,
                         boundary_repulsion_kb=1e-9),
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
            _base_params(max_iterations=5, attraction_ka=0.0,
                         courtyard_repulsion_kc=1e-9,
                         boundary_repulsion_kb=1e-9),
        )
        moved = apply_deltas(model, prop.deltas)
        assert moved.by_ref["B"].x_mm > 10.0
        assert moved.by_ref["A"].x_mm < 0.0


@pytest.mark.physics
class TestCourtyardRole:
    """Courtyard collision is what separates bodies; pad Coulomb alone is
    too weak at body scale (and singular at pad scale)."""

    def test_without_courtyard_overlap_persists(self):
        model = _overlap(dx=1.0)
        before = overlap_area(model, {})
        prop = run_placement(
            model,
            _base_params(max_iterations=5, repulsion_kr=0.01,
                         courtyard_repulsion_kc=1e-9),
        )
        after = overlap_area(model, prop.deltas)
        assert after > before * 0.5, (
            f"weak pad Coulomb alone must not resolve bodies ({before} -> {after})"
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

    def test_wall_holds_against_shove(self):
        def run(kb):
            model = _board([
                _single_pad("A", "uuid-a", -26.0, 0.0, "N1",
                            hw=0.25, hh=0.25),
                _single_pad("B", "uuid-b", -18.0, 0.0, "N2",
                            hw=0.25, hh=0.25),
            ])
            return run_placement(
                model,
                _base_params(max_iterations=10, boundary_repulsion_kb=kb,
                             courtyard_repulsion_kc=1e-9),
            ), model

        (walled, model) = run(20000.0)
        (open_, _) = run(1e-9)
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
            model, _base_params(max_iterations=30, repulsion_kr=1.0)
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
class TestRigidRole:
    """Rigid MST constraints keep a footprint's pads at rest length while
    external forces shove it around; without stiffness the body stretches."""

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
        fp = next(f for f in moved.footprints if f.uuid == uuid)
        rows = uuid_to_idx[uuid]
        pts = pos[rows]
        return float(np.linalg.norm(pts[0] - pts[-1]))

    def test_stiff_body_holds_shape(self):
        model = self._shear_board()
        before = self._pair_span(model, {}, "uuid-m")
        prop = run_placement(model, _base_params(max_iterations=5))
        after = self._pair_span(model, prop.deltas, "uuid-m")
        assert after == pytest.approx(before, rel=1e-3)

    def test_limp_body_diverges(self):
        """Without stiffness the pads shear independently under the same
        differential push, so the trajectory must differ from the stiff
        run.         (Pad-level stretch itself is not observable: proposals carry
        a rigid pose per footprint, which is exactly what the stiff test
        above pins.) Budget is 30, not 5: under the fixed step cap a
        saturated blast moves at full budget every iteration regardless
        of stiffness, so short runs are stiffness-blind by construction;
        the runs must reach the unsaturated settling phase to diverge."""
        model = self._shear_board()
        stiff = run_placement(model, _base_params(max_iterations=30))
        limp = run_placement(
            model, _base_params(max_iterations=30, rigid_stiffness=1e-9)
        )
        dx_stiff, _, _ = stiff.deltas["uuid-m"]
        dx_limp, _, _ = limp.deltas["uuid-m"]
        assert abs(dx_stiff - dx_limp) > 1e-6, (
            f"stiffness must matter under shear ({dx_stiff} vs {dx_limp})"
        )
