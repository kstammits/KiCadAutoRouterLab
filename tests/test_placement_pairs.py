"""Movable-vs-immobile courtyard pairing + bounded boundary push.

Regression tests for the 2026-10-10 R11-into-RV2 incident on DCCF:

1. The courtyard pair loop was ``for j in range(i + 1, ...)`` under
   ``if not movable[i]: continue``, so a movable footprint never felt an
   immobile (locked/unselected) footprint sorting earlier in file order —
   a lone selected part collided with only ~half the board.
2. The near-edge boundary term ``kb*2*(t-d)/(d+eps)`` had a singularity
   (~3.5e7 measured on a part 0.03mm inside the edge); it is now a bounded
   linear falloff via ``_boundary_edge_push`` (max 2*kb).

Board builders mirror tests/test_placement_layers.py (valid head-to-tail
courtyard rects, so these tests isolate the pairing/boundary logic from
the known courtyard-shape debt noted in TODO.md).
"""

import math

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    Point,
)
from kicad_autorouter.placement import (
    PlacementParams,
    _boundary_edge_push,
    run_placement,
)

from .helpers import make_fp, overlap_area


def _fp(ref, uuid, x, locked, net_a, net_b, hw=3.0, hh=2.0):
    # Board-space courtyards (production layout: outlines sit at the
    # footprint's board position). Local-origin outlines would both pose at
    # the origin on the first iteration (the rigid pose only tracks deltas),
    # collapsing the initial relative geometry to fully coincident and
    # tripping the containment dist~0 guard -> 0 force. Next-step (F0)
    # reporting needs the true initial geometry, so board-space it is.
    return make_fp(ref, uuid, x, 0.0, (net_a, net_b),
                   footprint_id="test:R", hw=hw, hh=hh,
                   pad_layers=("F.Cu",), pad_shape="circle",
                   locked=locked)


def _board(first_locked: bool):
    """Two 6x4mm parts 1mm apart (heavy courtyard overlap); file order varies.

    first_locked=True: locked A (x=0) sorts before movable B (x=1) — the
    R11/RV2 layout the old pair loop never evaluated.
    first_locked=False: movable B (x=0) sorts before locked A (x=1).
    """
    if first_locked:
        fa = _fp("A", "uuid-a", 0.0, True, "N1", "N2")
        fb = _fp("B", "uuid-b", 1.0, False, "N3", "N4")
        fps = (fa, fb)
    else:
        fb = _fp("B", "uuid-b", 0.0, False, "N3", "N4")
        fa = _fp("A", "uuid-a", 1.0, True, "N1", "N2")
        fps = (fb, fa)
    edge = 30.0
    return BoardModel(
        footprints=fps,
        keepout_zones=(),
        nets={},
        by_ref={"A": fa, "B": fb},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={"uuid-a": 0, "uuid-b": 0},
    )


def _overlap_area(model: BoardModel, deltas: dict) -> float:
    return overlap_area(model, deltas)


PARAMS = PlacementParams(
    max_iterations=10,
    convergence_eps_mm=1e-12,
    repulsion_kr=100.0,
    attraction_ka=0.05,
    ideal_length_mm=25.0,
    courtyard_repulsion_kc=10000.0,
    courtyard_halo_mm=3.0,
    boundary_repulsion_kb=1.0,  # negligible: parts start at board center
    rigid_stiffness=5e5,
)


SEPARATION_PARAMS = PlacementParams(
    # Short horizon on purpose: a few iterations already carry the full
    # signal (pair evaluated, ~1e5 force, ~mm motion), while the
    # long-horizon stiff-contact regime oscillates and would make the
    # test about integrator dynamics instead of pairing. Thresholds
    # calibrated by running: old code reports EXACTLY 0.0 / no motion.
    max_iterations=3,
    convergence_eps_mm=1e-12,
    repulsion_kr=100.0,
    attraction_ka=0.05,
    ideal_length_mm=25.0,
    courtyard_repulsion_kc=10000.0,
    courtyard_halo_mm=3.0,
    boundary_repulsion_kb=1.0,
    rigid_stiffness=5e5,
)


@pytest.mark.regression
def test_movable_separates_from_earlier_immobile():
    """The R11/RV2 case: immobile part sorts FIRST, only B movable."""
    model = _board(first_locked=True)
    before = _overlap_area(model, {})
    assert before > 10.0
    prop = run_placement(model, SEPARATION_PARAMS, movable_uuids={"uuid-b"})
    assert _overlap_area(model, prop.deltas) < before  # pushed apart
    # Locked part untouched, movable part pushed east away from it.
    assert "uuid-a" not in prop.deltas
    dx, _, _ = prop.deltas["uuid-b"]
    assert dx > 0.0
    comps = prop.force_components["uuid-b"]
    assert set(comps) == {"repulsion", "attraction", "rigid", "ghost", "courtyard", "boundary"}
    cx, _ = comps["courtyard"]
    assert cx > 1e4  # old code: exactly 0.0 (pair never evaluated)


@pytest.mark.regression
def test_movable_separates_from_later_immobile():
    """The arm that always worked (j > i): guard the refactor kept it."""
    model = _board(first_locked=False)
    before = _overlap_area(model, {})
    assert before > 10.0
    prop = run_placement(model, SEPARATION_PARAMS, movable_uuids={"uuid-b"})
    assert _overlap_area(model, prop.deltas) < before
    assert "uuid-a" not in prop.deltas
    dx, _, _ = prop.deltas["uuid-b"]
    assert dx < 0.0  # B sits west of A here: pushed west
    cx, _ = prop.force_components["uuid-b"]["courtyard"]
    assert cx < -1e4  # old code: exactly 0.0 (pair never evaluated)


@pytest.mark.regression
def test_rotation_rate_capped_per_iteration():
    """F2: no footprint spins more than max_dangle_deg in one iteration.

    A locked part sits ~0.3mm from one pad of a 2-pad movable part, so
    per-pad repulsion differs by ~50x across the footprint -- without a
    cap the translation budget synthesizes tens of degrees of spin in a
    single step, which Kabsch only reads out after the damage is done.
    Courtyard kc is set near-zero on purpose: courtyard pushes are
    stretch-dominated (both pads shoved along one axis, which Kabsch
    reads as extension, not rotation) and already small here -- this
    test isolates rotational shear. The cap bounds the rate (15deg
    default still allows full reorientation over a run, which magnet
    clustering needs); rotation is never frozen.
    """
    from .helpers import make_fp as _make_fp

    mover = _make_fp("M", "uuid-m", 2.0, 0.0, ("N1", "N2"),
                     footprint_id="test:R", hw=1.5, hh=1.0,
                     pad_layers=("F.Cu",), pad_shape="circle")
    locked = _make_fp("L", "uuid-l", 1.0, 0.3, ("N9",),
                      footprint_id="test:R", hw=1.5, hh=1.0,
                      pad_layers=("F.Cu",), pad_shape="circle",
                      locked=True)
    edge = 30.0
    model = BoardModel(
        footprints=(mover, locked),
        keepout_zones=(),
        nets={},
        by_ref={"M": mover, "L": locked},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={"uuid-m": 0, "uuid-l": 0},
    )
    base = dict(
        max_iterations=1,
        convergence_eps_mm=1e-12,
        repulsion_kr=100.0,
        attraction_ka=0.05,
        ideal_length_mm=25.0,
        courtyard_repulsion_kc=1e-6,  # near-zero: isolate repulsion shear
        courtyard_halo_mm=3.0,
        boundary_repulsion_kb=1.0,
        rigid_stiffness=5e5,
    )
    capped = run_placement(
        model, PlacementParams(**base, max_dangle_deg=15.0),
        movable_uuids={"uuid-m"},
    )
    assert "uuid-m" in capped.deltas
    _, _, da_capped = capped.deltas["uuid-m"]
    assert abs(da_capped) <= 15.0 + 1e-6, f"rotation exceeded cap: da={da_capped}"
    # Control: cap disabled (>= 180) must show the shear the cap removes.
    free = run_placement(
        model, PlacementParams(**base, max_dangle_deg=200.0),
        movable_uuids={"uuid-m"},
    )
    _, _, da_free = free.deltas["uuid-m"]
    assert abs(da_free) > 15.0, (
        f"expected uncapped spin above the cap, got da={da_free} "
        f"(capped was {da_capped})"
    )


@pytest.mark.regression
def test_contact_weighting_conserves_total():
    """F4: the contact-weighted spread conserves the footprint total.

    B (2 pads) and a single-pad probe S share the identical courtyard
    layout relative to locked A, so S must feel exactly half of B's
    courtyard component (weights average 1 by construction). Repulsion
    differs by pad count, so only courtyard components are compared.
    """
    from .helpers import make_fp as _make_fp

    model = _board(first_locked=True)
    params = PlacementParams(
        max_iterations=0,
        convergence_eps_mm=1e-12,
        repulsion_kr=100.0,
        attraction_ka=0.05,
        ideal_length_mm=25.0,
        courtyard_repulsion_kc=10000.0,
        courtyard_halo_mm=3.0,
        boundary_repulsion_kb=1.0,
        rigid_stiffness=5e5,
    )
    prop = run_placement(model, params, movable_uuids={"uuid-b"})
    total_fx, _ = prop.forces["uuid-b"]
    assert total_fx > 1e4  # sanity: the pair push exists
    single = _make_fp("S", "uuid-s", 1.0, 0.0, ("N3",),
                      footprint_id="test:R", hw=3.0, hh=2.0,
                      pad_layers=("F.Cu",), pad_shape="circle")
    locked_a = _fp("A", "uuid-a", 0.0, True, "N1", "N2")
    edge = 30.0
    model1 = BoardModel(
        footprints=(locked_a, single),
        keepout_zones=(),
        nets={},
        by_ref={"A": locked_a, "S": single},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={"uuid-a": 0, "uuid-s": 0},
    )
    prop1 = run_placement(model1, params, movable_uuids={"uuid-s"})
    cy_b = prop.force_components["uuid-b"]["courtyard"][0]
    cy_s = prop1.force_components["uuid-s"]["courtyard"][0]
    assert cy_s == pytest.approx(cy_b / 2.0, rel=1e-9), (
        f"weighting changed the total: single={cy_s}, half-of-B={cy_b / 2.0}"
    )


@pytest.mark.regression
def test_contact_weights_unit():
    """_contact_weights: contact side loaded, mean exactly 1, single -> 1."""
    import numpy as np

    from kicad_autorouter.placement import _contact_weights

    w = _contact_weights(np.array([-0.5, 0.5]))
    assert w[0] > w[1]  # contact side (negative proj) takes more
    assert sum(w) == pytest.approx(2.0)  # mean exactly 1: total conserved
    assert _contact_weights(np.array([0.0])) == pytest.approx([1.0])
    assert sum(_contact_weights(np.array([-0.9, -0.1, 0.4]))) == pytest.approx(3.0)


@pytest.mark.regression
def test_boundary_edge_push_bounded():
    """No singularity at the edge; saturating pull outside; zero past reach."""
    kb, t = 500000.0, 1.0
    # At the R11 incident distance the old formula gave ~3.2e7; now < 2*kb.
    assert _boundary_edge_push(0.03, t, kb) == pytest.approx(kb * 2.0 * 0.97 / t)
    assert _boundary_edge_push(0.03, t, kb) < 2.0 * kb
    assert _boundary_edge_push(0.0, t, kb) == pytest.approx(2.0 * kb)
    assert _boundary_edge_push(t / 2.0, t, kb) == pytest.approx(kb)
    assert _boundary_edge_push(t, t, kb) == 0.0
    assert _boundary_edge_push(t + 5.0, t, kb) == 0.0
    outside = _boundary_edge_push(-50.0, t, kb)
    assert 0.0 < outside <= kb  # saturates, never explodes


@pytest.mark.regression
def test_boundary_force_bounded_near_edge():
    """Integration: a 60x40mm part starting 0.5mm inside the left edge on a
    wide board. One capped (2mm) step cannot clear its ~7.4mm threshold
    zone, so the reported final force still reflects the near-edge regime:
    positive (pushing +x, away) and within the 2*kb-per-pad bound. The old
    singular formula reports ~2x over the bound here (and ~35x at 0.03mm).
    """
    part = _fp("P", "uuid-p", -2.44, False, "N1", "N2", hw=30.0, hh=20.0)
    x0, x1, y0, y1 = -40.0, 200.0, -60.0, 60.0
    model = BoardModel(
        footprints=(part,),
        keepout_zones=(),
        nets={},
        by_ref={"P": part},
        edge_cuts=(
            (Point(x0, y0), Point(x1, y0)),
            (Point(x1, y0), Point(x1, y1)),
            (Point(x1, y1), Point(x0, y1)),
            (Point(x0, y1), Point(x0, y0)),
        ),
        footprint_region={"uuid-p": 0},
    )
    params = PlacementParams(
        max_iterations=1,
        convergence_eps_mm=1e-12,
        repulsion_kr=100.0,
        attraction_ka=0.05,
        ideal_length_mm=25.0,
        courtyard_repulsion_kc=10000.0,
        courtyard_halo_mm=3.0,
        boundary_repulsion_kb=500000.0,
        rigid_stiffness=5e5,
    )
    prop = run_placement(model, params)
    bx, by = prop.force_components["uuid-p"]["boundary"]
    assert math.isfinite(bx) and math.isfinite(by)
    assert by == 0.0  # far from top/bottom edges
    # Per-pad bound is 2*kb; the footprint component sums over its pads.
    assert 0.0 < bx <= 2.0 * params.boundary_repulsion_kb * len(part.pads)
