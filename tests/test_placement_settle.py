"""Honest stopping: never report rest while pushes remain unserved.

Regression family for the 2026-10-16 DCCF fake-settle: the displacement-only
convergence check (`max pad step < eps`) fired while ~0.1N wall/attraction
forces persisted, because the adaptive-dt guard (halve + velocity bleed) had
strangled sustained-contact motion into a sub-epsilon crawl. The run
reported a quiet settle with parts still buried in each other.

Pinned contract (see PlacementProposal.stop_reason):
- "converged" requires BOTH a quiet step AND residual forces below the
  settle scale (post-friction, so deadband-frozen rest still terminates).
- "stalled" ends a run whose window-averaged pose shows no net progress
  over a full stall window while forces remain unserved (wedged, or
  chattering in place with no resolvable drift): wedged, not rested —
  headless callers must treat it like budget, never like converged.
- max_iterations stays the backstop: force-balanced wedges report "budget"
  with their residual attached, never "converged".
"""

import math

import pytest

from kicad_autorouter.board_model import BoardModel, Point, apply_deltas
from kicad_autorouter.placement import PlacementParams, run_placement

from .helpers import make_fp, overlap_area
from .test_courtyard_behavior import make_rv1_like


def _board(fps, edge=30.0):
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def _deep_pair(x1=1.0):
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", x1, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    return _board([fa, fb])


@pytest.mark.physics
def test_buried_pair_ignores_coarse_eps_and_clears():
    """Eps coarser than the step cap must not stop iteration 1 with overlap.

    Old displacement-only code broke immediately here (first step is capped
    at vmax*dt = 1mm < eps = 10mm); the force gate must keep the run alive
    until the 20mm² overlap actually clears.
    """
    model = _deep_pair()
    assert overlap_area(model, {}) > 15.0
    prop = run_placement(
        model,
        PlacementParams(max_iterations=100, convergence_eps_m=0.01,
                        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                        wall_k_N=0.08, wall_halo_m=0.006,
                         ghost_k_N=1e-15,
                        damping_Ns_m=0.12),
    )
    assert overlap_area(model, prop.deltas) < 1e-6
    # The force gate must keep the run alive until overlap clears: with a
    # healthy timestep the overlap clears fast and the run may then converge
    # (both gates pass) before the budget is spent. Either outcome proves the
    # force gate did its job — what must NOT happen is stopping at iteration 1
    # with overlap remaining.
    assert prop.iterations > 1
    if prop.stop_reason == "converged":
        assert prop.residual_max_force_N < 1e-3
    else:
        # Budget or stall: either way the run admits work remains (stalled
        # = quiet with unserved forces; budget = still moving at the cap).
        assert prop.stop_reason in ("budget", "stalled")
        if prop.stop_reason == "budget":
            assert prop.iterations == 100


@pytest.mark.physics
def test_settled_tail_reports_converged():
    """A genuinely finished run still early-stops and says so.

    Separated pair cruising down the Coulomb tail: friction freezes it,
    post-friction residual reads 0, displacement reads 0 → converged.
    """
    # Start parts adjacent (outside wall halo): hw=3mm each, halo=6mm
    # Part A at 0, Part B at 12 → gap = 9mm > halo (6mm), no wall force
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", 12.0, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([fa, fb])
    prop = run_placement(model, PlacementParams())
    assert prop.stop_reason == "converged"
    assert prop.iterations < 1000  # default budget
    assert prop.residual_max_force_N < 1e-3
    assert overlap_area(model, prop.deltas) < 1e-6


@pytest.mark.physics
def test_friction_frozen_reports_converged_empty():
    """Deadband-frozen rest reads zero residual → converged, not budget."""
    model = _deep_pair(x1=60.0)
    prop = run_placement(
        model,
        PlacementParams(max_iterations=10, convergence_eps_m=1e-15,
                        friction_N=1e3),
    )
    assert prop.deltas == {}
    assert prop.stop_reason == "converged"


@pytest.mark.physics
def test_buried_in_rv1_keeps_working_until_out():
    """Resistor centered in a locked RV1 courtyard must travel out, not
    settle inside: every step carries ~mN wall forces, so no early stop is
    honest until the overlap is gone."""
    rv1 = make_rv1_like("RV1", "uuid-rv1", 0.0, 0.0, 0.0, locked=True)
    resistor = make_fp("R1", "uuid-r1", 37.0, 1.75, ("NET_A", "NET_B"),
                       hw=1.5, hh=1.0, pad_layers=("F.Cu",))
    model = _board([rv1, resistor], edge=100.0)
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
def test_loaded_contact_settles_despite_per_pad_loads():
    """A part pressed into contact must converge once its NET force settles.

    Movable A is pulled by a shared net into locked B and rests pressed
    against B's courtyard: the wall push concentrates on the contact pads
    while attraction spreads over all pads, so per-pad loads stay at mN
    level forever. The residual gate reads net force per footprint (rigid
    bodies settle as wholes — per-pad max can never vanish under contact
    load), so this run must report converged, not budget. Regression for
    the 2B Q1 legalize, which chattered to budget with the per-pad gate.
    """
    fa = make_fp("A", "uuid-a", 2.0, 0.0, ("N1", "NX"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", 10.0, 0.0, ("N1", "NY"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle",
                 locked=True)
    # A spans x -1..5, B spans 7..13: 2mm halo gap. Shared N1 pulls A
    # east into B; the wall holds it off at a hover gap.
    model = _board([fa, fb], edge=100.0)
    prop = run_placement(
        model,
        PlacementParams(max_iterations=500,
                        repulsion_k_N_m2=1e-15, attraction_k_N=0.03,
                        wall_k_N=0.05, wall_halo_m=0.006),
    )
    assert prop.stop_reason == "converged", (
        f"net-settled contact must converge, got {prop.stop_reason} "
        f"with residual {prop.residual_max_force_N:.6f}"
    )
    assert prop.residual_max_force_N < 1e-3
    assert overlap_area(model, prop.deltas) < 0.5


@pytest.mark.physics
def test_quiet_without_progress_stops_stalled():
    """No net progress of the window-averaged pose over a full stall window
    stops early as stalled.

    With a coarse eps the overlapping pair's mm-scale motion never moves
    the window mean anywhere near the 400mm stall tolerance, so the run
    reports "stalled" with residual attached after exactly one window
    instead of burning the whole budget. Regression for the 2B Q1 legalize,
    which sat at the wall for 900 idle iterations reporting budget.
    """
    model = _deep_pair()
    prop = run_placement(
        model,
        PlacementParams(max_iterations=1000, convergence_eps_m=0.1,
                        stall_window=10),
    )
    assert prop.stop_reason == "stalled"
    assert prop.iterations == 10
    assert prop.residual_max_force_N > 0.0


@pytest.mark.regression
@pytest.mark.dccf
def test_r5_legalize_chatter_stops_stalled(dccf_model):
    """Single-part R5 legalize must stall, not burn budget chattering.

    R5 slides into a locked neighbor and then jitters in place (~80um
    steps, ~1.5um window-mean drift) for the rest of the budget. The old
    consecutive-quiet stall gate never fired because no single step stays
    below eps; the window-mean rule sees the center is not going anywhere
    and stops as "stalled" with the unserved contact force attached.
    Regression for the DCCF Legalize+R5 run that spent its last 500 of
    1000 iterations jittering against a locked part.
    """
    r5 = dccf_model.by_ref["R5"]
    prop = run_placement(
        dccf_model,
        PlacementParams(max_iterations=1000,
                        attraction_k_N=0.008, wall_k_N=0.09,
                        damping_Ns_m=0.13, dt_s=1e-3,
                        vmax_m_s=1.0, friction_N=1e-6),
        movable_uuids={r5.uuid},
    )
    assert prop.stop_reason == "stalled"
    assert prop.iterations < 1000
    assert prop.residual_max_force_N > 1e-3
