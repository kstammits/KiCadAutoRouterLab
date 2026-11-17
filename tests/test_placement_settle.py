"""Honest stopping: never report rest while pushes remain unserved.

A displacement-only convergence check (`max pad step < eps`) can fire while
wall/attraction forces persist, because the adaptive-dt guard (halve +
velocity bleed) can strangle sustained-contact motion into a sub-epsilon
crawl. The run must not report a quiet settle with parts still buried in
each other.

Pinned contract (see PlacementProposal.stop_reason):
- "converged" requires a quiet step AND residual forces below the
  settle scale (post-friction, so deadband-frozen rest still terminates),
  OR a dead window-averaged drift with quiet residual (limit-cycle
  chatter whose center sits still with nothing left to obey).
- "stalled" ends a run whose window-averaged pose shows no net progress
  over a full stall window while forces remain unserved (wedged, or
  chattering in place with no resolvable drift): wedged, not rested —
  headless callers must treat it like budget, never like converged.
- max_iterations stays the backstop: force-balanced wedges report "budget"
  with their residual attached, never "converged".
"""

import pytest

from kicad_autorouter.board_model import BoardModel, Point
from kicad_autorouter.placement import PlacementParams, run_placement

from .helpers import make_board, make_fp, overlap_area
from .test_courtyard_behavior import make_rv1_like


def _deep_pair(x1=1.0):
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", x1, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    return make_board([fa, fb])


@pytest.mark.physics
@pytest.mark.unit
@pytest.mark.parametrize("case", ["tail", "frozen", "contact"])
def test_honest_converged(case):
    """Free, deadband-frozen, and contact-loaded runs all say converged.

    tail: separated pair on the Coulomb tail — friction freezes it,
    residual reads 0. frozen: deadband-frozen rest reads zero residual,
    not budget. contact: net-settled contact (rigid bodies settle as
    wholes) converges despite mN per-pad loads.
    """
    if case == "tail":
        # Parts adjacent (outside wall halo): hw=3mm each, halo=6mm.
        # A at 0, B at 12 → gap 9mm > halo, no wall force.
        fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle")
        fb = make_fp("B", "uuid-b", 12.0, 0.0, ("N3", "N4"),
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle")
        model = make_board([fa, fb])
        prop = run_placement(model, PlacementParams())
        assert prop.stop_reason == "converged"
        assert prop.iterations < 1000  # default budget
        assert prop.residual_max_force_N < 1e-3
        assert overlap_area(model, prop.deltas) < 1e-6
    elif case == "frozen":
        model = _deep_pair(x1=60.0)
        prop = run_placement(
            model,
            PlacementParams(max_iterations=10, convergence_eps_m=1e-15,
                            friction_N=1e3),
        )
        assert prop.deltas == {}
        assert prop.stop_reason == "converged"
    else:
        # Movable A pulled by a shared net into locked B, resting pressed
        # against B's courtyard. Tangential assist off so the symmetric
        # 1D contact isolates the normal-force balance.
        fa = make_fp("A", "uuid-a", 2.0, 0.0, ("N1", "NX"),
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle")
        fb = make_fp("B", "uuid-b", 10.0, 0.0, ("N1", "NY"),
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle",
                     locked=True)
        # A spans x -1..5, B spans 7..13: 2mm halo gap. Shared N1 pulls A
        # east into B; the wall holds it off at a hover gap.
        model = make_board([fa, fb], edge=100.0)
        prop = run_placement(
            model,
            PlacementParams(max_iterations=500,
                            repulsion_k_N_m2=1e-15, attraction_k_N=0.03,
                            wall_k_N=0.05, wall_halo_m=0.006,
                            contact_tangent_gamma=0.0),
        )
        assert prop.stop_reason == "converged", (
            f"net-settled contact must converge, got {prop.stop_reason} "
            f"with residual {prop.residual_max_force_N:.6f}"
        )
        assert prop.residual_max_force_N < 1e-3
        assert overlap_area(model, prop.deltas) < 0.5


@pytest.mark.physics
@pytest.mark.unit
def test_buried_in_rv1_keeps_working_until_out():
    """Resistor centered in a locked RV1 courtyard must travel out, not
    settle inside: every step carries ~mN wall forces, so no early stop is
    honest until the overlap is gone."""
    rv1 = make_rv1_like("RV1", "uuid-rv1", 0.0, 0.0, 0.0, locked=True)
    resistor = make_fp("R1", "uuid-r1", 37.0, 1.75, ("NET_A", "NET_B"),
                       hw=1.5, hh=1.0, pad_layers=("F.Cu",))
    model = make_board([rv1, resistor], edge=100.0)
    prop = run_placement(
        model,
        PlacementParams(max_iterations=300, convergence_eps_m=1e-12,
                        repulsion_k_N_m2=1e-7, attraction_k_N=5e-5,
                        wall_k_N=0.002, wall_halo_m=0.003,
                        ),
    )
    assert prop.iterations > 1
    dx, dy, _ = prop.deltas.get("uuid-r1", (0.0, 0.0, 0.0))
    final = (37.0 + dx, 1.75 + dy)
    outside = (final[0] < -2.75 or final[0] > 76.25
               or final[1] < -4.25 or final[1] > 7.75)
    assert outside, f"buried resistor must escape RV1, ended at {final}"


@pytest.mark.regression
@pytest.mark.dccf
@pytest.mark.slow
def test_dccf_legalize_stop_is_honest(dccf_model):
    """Full-DCCF legalize run: if overlaps/forces remain at the budget, the
    proposal must say 'budget' with a nonzero residual — headless callers
    rely on this instead of a silent fake settle."""
    prop = run_placement(
        dccf_model,
        PlacementParams(max_iterations=200, convergence_eps_m=2e-5,
                        attraction_k_N=0.008, wall_k_N=0.09,
                        damping_Ns_m=0.13, dt_s=1e-3),
    )
    assert prop.stop_reason in ("converged", "budget", "stalled")
    if prop.stop_reason == "converged":
        assert prop.residual_max_force_N < 1e-3
    else:
        # Known DCCF wedges persist: the run must admit work remains.
        assert prop.residual_max_force_N > 1e-3, (
            "budget-out with no residual force is a fake settle")


@pytest.mark.physics
@pytest.mark.unit
@pytest.mark.parametrize("case", ["quiet", "overlap", "ghost", "wall"])
def test_honest_stalled(case):
    """Stuck runs say stalled, with the mechanism attached.

    quiet: no window progress over a full stall window. overlap: illegal
    courtyard overlap. ghost: movable overlapping a ghost is legal
    (placement-only pseudo-part) — converges with an audit exemption.
    wall: movable straddling the region edge.
    """
    from kicad_autorouter.board_model import BoardRegion, Footprint

    if case == "quiet":
        # Coarse eps: the overlapping pair's mm-scale motion never moves
        # the window mean near the 400mm stall tolerance — one window,
        # then stalled with residual attached.
        model = _deep_pair()
        prop = run_placement(
            model,
            PlacementParams(max_iterations=1000, convergence_eps_m=0.1,
                            stall_window=10),
        )
        assert prop.stop_reason == "stalled"
        assert prop.iterations == 10
        assert prop.residual_max_force_N > 0.0
    elif case == "overlap":
        # Two movable squares with shared net overlapping past tolerance.
        fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle")
        fb = make_fp("B", "uuid-b", 1.0, 0.0, ("N1", "N4"),  # Shared net N1
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle")
        model = make_board([fa, fb], edge=30.0)
        assert overlap_area(model, {}) > 10.0
        prop = run_placement(
            model,
            PlacementParams(max_iterations=200, convergence_eps_m=1e-3,
                            repulsion_k_N_m2=1e-15, attraction_k_N=0.5,
                            wall_k_N=0.02, wall_halo_m=0.006,
                            ghost_k_N=1e-15, damping_Ns_m=0.12),
        )
        assert prop.stop_reason == "stalled", (
            f"illegal overlap must stall, got {prop.stop_reason}")
        assert not prop.legality_is_legal
        assert len(prop.overlapping_pairs) == 1
        ua, ub, area = prop.overlapping_pairs[0]
        assert {ua, ub} == {"uuid-a", "uuid-b"}
        assert area > 0.1
    elif case == "ghost":
        # Movable overlapping a ghost is legal: ghosts are placement-only
        # pseudo-parts, so the run converges and the pair is exempted
        # (audit only) instead of stalling on an illegal overlap.
        fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle")
        ghost = Footprint(
            ref="GW1", footprint_id="test:wall", layer="F.Cu",
            x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=(),
            courtyard=(
                (Point(-5, -5), Point(5, -5)),
                (Point(5, -5), Point(5, 5)),
                (Point(5, 5), Point(-5, 5)),
                (Point(-5, 5), Point(-5, -5)),
            ),
            locked=False, uuid="uuid-gw", ghost=True, ghost_attractions=(),
        )
        model = make_board([fa, ghost], edge=30.0)
        assert overlap_area(model, {}, uuids=("uuid-a", "uuid-gw")) > 10.0
        prop = run_placement(
            model,
            PlacementParams(max_iterations=100, convergence_eps_m=1e-3,
                            repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                            wall_k_N=0.08, wall_halo_m=0.006,
                            ghost_k_N=1e-15, damping_Ns_m=0.12),
        )
        assert prop.stop_reason == "converged"
        assert prop.legality_is_legal
        assert len(prop.overlapping_pairs) == 0
        assert ("uuid-a", "uuid-gw") in prop.exempt_pairs or ("uuid-gw", "uuid-a") in prop.exempt_pairs
    else:
        # Movable straddling the region edge, pulled out by a ghost.
        fa = make_fp("A", "uuid-a", 15.0, 0.0, ("N1", "N2"),
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle")
        ghost = Footprint(
            ref="GW1", footprint_id="test:wall", layer="F.Cu",
            x_mm=30.0, y_mm=0.0, angle_deg=0.0, pads=(),
            courtyard=(
                (Point(25, -5), Point(35, -5)),
                (Point(35, -5), Point(35, 5)),
                (Point(35, 5), Point(25, 5)),
                (Point(25, 5), Point(25, -5)),
            ),
            locked=False, uuid="uuid-gw", ghost=True,
            ghost_attractions=(("A", 0.5),),  # Pull A toward x=30
        )
        # Board x in [-20, 20], ghost at x=30 (outside)
        region_poly = (
            Point(-20, -20), Point(20, -20),
            Point(20, 20), Point(-20, 20), Point(-20, -20)
        )
        region = BoardRegion(
            uuid='region-0',
            polygon=region_poly,
            bbox=(-20.0, 20.0, -20.0, 20.0)
        )
        model = BoardModel(
            footprints=(fa,),
            ghost_footprints=(ghost,),
            keepout_zones=(),
            nets={},
            by_ref={fa.ref: fa},
            edge_cuts=(
                (Point(-20, -20), Point(20, -20)),
                (Point(20, -20), Point(20, 20)),
                (Point(20, 20), Point(-20, 20)),
                (Point(-20, 20), Point(-20, -20)),
            ),
            board_regions=(region,),
            footprint_region={fa.uuid: 0, ghost.uuid: 0},
        )
        prop = run_placement(
            model,
            PlacementParams(max_iterations=200, convergence_eps_m=1e-3,
                            wall_k_N=0.05, wall_halo_m=0.006,
                            ghost_k_N=1.0, damping_Ns_m=0.12),
        )
        assert prop.stop_reason == "stalled", (
            f"wall penetration must stall, got {prop.stop_reason}")
        assert not prop.legality_is_legal
        assert len(prop.wall_violations) == 1
        uuid, depth = prop.wall_violations[0]
        assert uuid == "uuid-a"
        assert depth > 0.1


# --- Legality tests (per docs/legality_spec.md) ---

@pytest.mark.physics
@pytest.mark.unit
def test_ghost_over_locked_is_exempt():
    """Ghost overlapping a locked part is exempt by design."""
    from kicad_autorouter.board_model import Footprint
    fb = make_fp("B", "uuid-b", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    ghost = Footprint(
        ref="GW1", footprint_id="test:wall", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=(),
        courtyard=(
            (Point(-5, -5), Point(5, -5)),
            (Point(5, -5), Point(5, 5)),
            (Point(5, 5), Point(-5, 5)),
            (Point(-5, 5), Point(-5, -5)),
        ),
        locked=False, uuid="uuid-gw", ghost=True, ghost_attractions=(),
    )
    model = make_board([fb, ghost], edge=30.0)
    prop = run_placement(
        model,
        PlacementParams(max_iterations=10, convergence_eps_m=1e-3),
    )
    # Ghost-locked overlap is exempt, so no overlapping_pairs recorded
    assert prop.legality_is_legal
    assert len(prop.overlapping_pairs) == 0
    # Exempt pair should be recorded
    assert ("uuid-b", "uuid-gw") in prop.exempt_pairs or ("uuid-gw", "uuid-b") in prop.exempt_pairs


@pytest.mark.physics
@pytest.mark.unit
def test_ghost_attractor_over_movable_is_exempt():
    """Attractor square on its (movable) target is exempt, not illegal."""
    from kicad_autorouter.board_model import Footprint
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    ghost = Footprint(
        ref="GA1", footprint_id="test:attractor", layer="F.Cu",
        x_mm=0.0, y_mm=0.0, angle_deg=0.0, pads=(),
        courtyard=(
            (Point(-1, -1), Point(1, -1)),
            (Point(1, -1), Point(1, 1)),
            (Point(1, 1), Point(-1, 1)),
            (Point(-1, 1), Point(-1, -1)),
        ),
        locked=False, uuid="uuid-ga", ghost=True,
        ghost_attractions=(("A", 4000.0),),
    )
    model = make_board([fa, ghost], edge=30.0)
    prop = run_placement(
        model,
        PlacementParams(max_iterations=10, convergence_eps_m=1e-3),
    )
    # Attractor-on-target overlap is exempt, so no overlapping_pairs recorded
    assert prop.legality_is_legal
    assert len(prop.overlapping_pairs) == 0
    # Exempt pair should be recorded
    assert ("uuid-a", "uuid-ga") in prop.exempt_pairs or ("uuid-ga", "uuid-a") in prop.exempt_pairs


@pytest.mark.physics
@pytest.mark.unit
def test_depenetrated_flag_marks_post_loop_eject():
    """stop_reason must distinguish pure settle from settled-then-pushed.

    A budget-truncated run ends with overlap the penalty wall equilibrated
    against; the post-loop eject then moves parts geometrically. The
    proposal must say so (depenetrated=True) instead of letting "budget"
    imply the pose is where dynamics left it. A clean run reports False.
    """
    model = _deep_pair()
    prop = run_placement(model, PlacementParams(max_iterations=2, debug=True))
    assert prop.stop_reason == "budget"
    assert prop.depenetrated is True
    assert len(prop.debug_trace.get("depen", [])) > 0

    clean = _deep_pair(x1=60.0)
    frozen = run_placement(
        clean,
        PlacementParams(max_iterations=10, convergence_eps_m=1e-15,
                        friction_N=1e3),
    )
    assert frozen.stop_reason == "converged"
    assert frozen.depenetrated is False


@pytest.mark.physics
@pytest.mark.unit
def test_net_friction_settles_sustained_chatter():
    """Net-force friction rests a chattering squeezed part.

    A movable squeezed off-center between two locked walls (stiff wall,
    tangential sliding on) rocks on its contacts: per-pad loads stay
    mN-scale while the net force is small. Max-per-pad deadband never
    fires there, so the frictionless twin chatters to the full budget.
    With net-force friction the squeezed part freezes once its net and
    rim torque go quiet, motion dies, and the run converges — same
    board, same budget, honest stop.
    """
    def _squeezed():
        la = make_fp("L", "uuid-l", 0.0, 0.0, ("N1", "NX"),
                     footprint_id="test:R", hw=3.0, hh=2.0,
                     pad_layers=("F.Cu",), pad_shape="circle", locked=True)
        m = make_fp("M", "uuid-m", 7.0, 0.5, ("N1", "N2"),
                    footprint_id="test:R", hw=3.0, hh=2.0,
                    pad_layers=("F.Cu",), pad_shape="circle")
        r = make_fp("R", "uuid-r", 14.0, 0.0, ("N2", "NY"),
                    footprint_id="test:R", hw=3.0, hh=2.0,
                    pad_layers=("F.Cu",), pad_shape="circle", locked=True)
        return make_board([la, m, r], edge=100.0)
    kw = dict(max_iterations=500, convergence_eps_m=1e-9, stall_window=20,
              repulsion_k_N_m2=1e-15, attraction_k_N=0.05, wall_k_N=0.1,
              wall_halo_m=0.006, contact_tangent_gamma=0.15)
    rest = run_placement(_squeezed(), PlacementParams(**kw))
    assert rest.stop_reason == "converged", (
        f"squeezed rest must converge, got {rest.stop_reason} "
        f"with residual {rest.residual_max_force_N:.6f}")
    assert rest.residual_max_force_N < 1e-3
    loud = run_placement(_squeezed(), PlacementParams(friction_N=0.0, **kw))
    assert loud.stop_reason == "budget"
    assert loud.iterations == 500


@pytest.mark.physics
@pytest.mark.unit
def test_wall_slam_trips_local_energy_guard():
    """A light part buried in a stiff locked wall must not eject at full
    stride (R20-style wall hopping: a lone slam is invisible in the
    board-wide KE average, so the global guard never fires). A heavy
    locked ballast blinds the global guard on purpose (raises its
    mass-scaled floor while contributing no KE); the per-footprint guard
    still sees the slammer's own KE jump, halves the timestep, and the
    eject stays controlled. Asserted via the debug dt trace, not the
    stop reason.
    """
    def _slam():
        wall = make_fp("W", "uuid-w", 0.0, 0.0, ("NW1", "NW2"),
                       footprint_id="test:R", hw=4.0, hh=4.0,
                       pad_layers=("F.Cu",), pad_shape="rect", locked=True)
        ballast = make_fp("B", "uuid-b", 50.0, 50.0, ("NB1", "NB2"),
                          footprint_id="test:R", hw=25.0, hh=25.0,
                          pad_layers=("F.Cu",), pad_shape="rect", locked=True)
        # slammer overlaps the wall's +x edge by 1.5mm
        slam = make_fp("S", "uuid-s", 3.3, 0.0, ("NS1", "NS2"),
                       footprint_id="test:R", hw=0.8, hh=0.5,
                       pad_layers=("F.Cu",), pad_shape="rect")
        return make_board([wall, ballast, slam], edge=100.0)
    prop = run_placement(
        _slam(),
        PlacementParams(max_iterations=60, attraction_k_N=0.0,
                        repulsion_k_N_m2=0.0, wall_k_N=0.1, wall_halo_m=0.0005,
                        wall_exponent=2.0, ghost_k_N=0.0,
                        contact_tangent_gamma=0.0, friction_N=0.0,
                        damping_Ns_m=0.01, dt_s=2e-3,
                        align_angle_k_N=0.0, align_pos_k_N_m=0.0,
                        debug=True),
        {"uuid-s"})
    dts = [s["dt_s"] for s in prop.debug_trace["per_iteration"]]
    assert min(dts) < 2e-3, (
        f"local guard never fired (dt stayed {min(dts):.2e})")
    assert prop.deltas["uuid-s"][0] > 0.0, "eject must clear east, away from the wall"


@pytest.mark.physics
@pytest.mark.unit
@pytest.mark.parametrize("ke_now,ke_prev,floor,fires", [
    (1e-6, 1e-7, 1e-9, True),    # big relative + absolute jump
    (1.2e-6, 1e-6, 1e-9, False),  # 1.2x: below the relative gate
    (1e-13, 1e-14, 1e-15, False),  # below the noise floor
    (2e-6, 1e-6, 5e-6, False),    # relatively big but absolutely small
    (1e-6, 2e-6, 1e-9, False),    # falling energy never trips
])
def test_ke_jump_predicate_gates(ke_now, ke_prev, floor, fires):
    """The shared guard predicate fires only on significant jumps."""
    from kicad_autorouter.placement.region import _ke_jump_exceeds
    assert bool(_ke_jump_exceeds(ke_now, ke_prev, floor)) is fires


@pytest.mark.physics
@pytest.mark.unit
def test_guard_backoff_pins_chronic_hopper():
    """Isolated transients recover the timestep almost immediately, but a
    part that trips the guard every step must not regrow back to full
    stride between slams — the heat backoff pins dt down until the
    hopping actually stops.
    """
    from kicad_autorouter.placement.region import _guard_dt_update
    dt, heat = 2e-3, 0.0
    dt, heat = _guard_dt_update(dt, 5e-5, 2e-3, heat, True)
    assert dt == 1e-3 and heat == 1.0  # single trigger halves once
    dt, heat = _guard_dt_update(dt, 5e-5, 2e-3, heat, False)
    assert dt > 1e-3  # one quiet step resumes regrowth
    dt, heat = 2e-3, 0.0
    for _ in range(8):
        dt, heat = _guard_dt_update(dt, 5e-5, 2e-3, heat, True)
    assert dt == 5e-5, f"chronic hopping must pin dt at the floor, got {dt}"
    assert heat > 1.0
    for _ in range(250):
        dt, heat = _guard_dt_update(dt, 5e-5, 2e-3, heat, False)
    assert dt > 5e-5, "regrowth must resume once the hopping stops"


@pytest.mark.physics
@pytest.mark.unit
def test_uuidless_footprint_is_counted_not_silent():
    """Parts without a uuid are exempt from physics — surfaced, not silent.

    Warn-and-count policy: the run proceeds (strict rejection could refuse
    real boards with uuid-less graphical parts) but the proposal reports
    exactly how many footprints the solver never saw.
    """
    from .helpers import make_fp
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    ghost = make_fp("X", "", 50.0, 0.0, ("N9", "N10"),
                    footprint_id="test:R", hw=3.0, hh=2.0,
                    pad_layers=("F.Cu",), pad_shape="circle")
    model = make_board([fa, ghost])
    prop = run_placement(model, PlacementParams(max_iterations=10))
    assert prop.skipped_uuidless == 1
    # The uuid-less part never moved and never entered deltas:
    assert all(u for u in prop.deltas)
    assert "uuid-a" in prop.deltas or prop.deltas == {}

    clean = _deep_pair(x1=60.0)
    frozen = run_placement(
        clean,
        PlacementParams(max_iterations=10, convergence_eps_m=1e-15,
                        friction_N=1e3),
    )
    assert frozen.skipped_uuidless == 0
