"""Honest stopping: never report rest while pushes remain unserved.

Regression family for the 2026-10-16 DCCF fake-settle: the displacement-only
convergence check (`max pad step < eps`) fired while ~0.1N wall/attraction
forces persisted, because the adaptive-dt guard (halve + velocity bleed) had
strangled sustained-contact motion into a sub-epsilon crawl. The run
reported a quiet settle with parts still buried in each other.

Pinned contract (see PlacementProposal.stop_reason):
- "converged" requires BOTH a quiet step AND residual forces below the
  settle scale (post-friction, so deadband-frozen rest still terminates).
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
                        boundary_k_N=1e-15, ghost_k_N=1e-15,
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
        assert prop.stop_reason == "budget"
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
                        boundary_k_N=1e-6),
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
    assert prop.stop_reason in ("converged", "budget")
    if prop.stop_reason == "converged":
        assert prop.residual_max_force_N < 1e-3
    else:
        # Known DCCF wedges persist: the run must admit work remains.
        assert prop.residual_max_force_N > 1e-3, (
            "budget-out with no residual force is a fake settle")
