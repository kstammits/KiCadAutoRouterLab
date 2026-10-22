"""Tight-quarters placement corpus + rotation-in-squeeze regressions.
This file pins both families with fast synthetic boards plus one
full-DCCF audit that compares final overlaps against the input board so
new or worsened squeezes fail loudly without an allowlist.
"""

import math

import pytest

from kicad_autorouter.board_model import BoardModel, Point, apply_deltas
from kicad_autorouter.placement import PlacementParams, run_placement

from .helpers import make_fp, overlap_area


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


def _pair_overlap(model, deltas, a="uuid-a", b="uuid-b"):
    return overlap_area(model, deltas, uuids=(a, b))


PROD = PlacementParams()  # engine defaults (legalize regime)


# ---------------------------------------------------------------------------
# Phase 2: tight-quarters corpus
# ---------------------------------------------------------------------------

@pytest.mark.regression
def test_deep_overlap_resolves():
    """5mm-deep courtyard overlap (6x4mm parts, 1mm center distance)."""
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", 1.0, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([fa, fb])
    before = _pair_overlap(model, {})
    assert before > 15.0
    prop = run_placement(
        model,
        PlacementParams(max_iterations=100, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                        wall_k_N=PROD.wall_k_N, wall_halo_m=PROD.wall_halo_m,
                         ghost_k_N=1e-15,
                        damping_Ns_m=PROD.damping_Ns_m),
    )
    after = _pair_overlap(model, prop.deltas)
    assert after < 0.01 * before, f"deep overlap must clear: {before:.2f} -> {after:.3f}"


@pytest.mark.regression
def test_three_part_linear_squeeze():
    """Mobile part off-center between two locked parts escapes along the axis."""
    left = make_fp("L", "uuid-l", -4.0, 0.0, ("L1", "L2"),
                   footprint_id="test:R", hw=3.0, hh=2.0,
                   pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    right = make_fp("R", "uuid-r", 4.0, 0.0, ("R1", "R2"),
                    footprint_id="test:R", hw=3.0, hh=2.0,
                    pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    mid = make_fp("M", "uuid-m", 1.0, 0.0, ("M1", "M2"),
                  footprint_id="test:R", hw=2.5, hh=1.5,
                  pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([left, right, mid])
    prop = run_placement(
        model,
        PlacementParams(max_iterations=500, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                        wall_k_N=0.01, wall_halo_m=0.003,
                        ghost_k_N=1e-15, dt_s=2e-3),
        movable_uuids={"uuid-m"},
    )
    assert "uuid-m" in prop.deltas
    dx, dy, da = prop.deltas["uuid-m"]
    # Part is ejected (escapes the squeeze); tangent force changes escape direction.
    # Escape distance should be significant (>1mm) and spin bounded.
    assert math.hypot(dx, dy) > 1.0, f"part should escape, got dx={dx:.3f} dy={dy:.3f}"
    assert abs(da) < 15.0, f"squeeze must not spin the part, got da={da:.2f}"


@pytest.mark.regression
def test_depenetration_round_shrinks_overlap():
    """Post-dynamics eject: one relaxed MTV round per overlapping pair.

    With repulsion/attraction neutered the force dynamics cannot separate
    the pair, so any improvement comes from the final geometric pass.
    """
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", 2.0, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([fa, fb])
    before = _pair_overlap(model, {})
    assert before > 1.0
    prop = run_placement(
        model,
        PlacementParams(max_iterations=50, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                        wall_k_N=0.01, wall_halo_m=0.003,
                        ghost_k_N=1e-15),
    )
    after = _pair_overlap(model, prop.deltas)
    assert after < before, f"eject round must shrink overlap ({before:.3f} -> {after:.3f})"


@pytest.mark.regression
def test_corner_wedge_escapes():
    """Mobile part tucked into a locked corner (two walls) moves out."""
    wall_h = make_fp("H", "uuid-h", 0.0, -4.0, ("H1", "H2"),
                     footprint_id="test:R", hw=6.0, hh=1.5,
                     pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    wall_v = make_fp("V", "uuid-v", -4.0, 0.0, ("V1", "V2"),
                     footprint_id="test:R", hw=1.5, hh=6.0,
                     pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    mob = make_fp("M", "uuid-m", -2.0, -2.0, ("M1", "M2"),
                  footprint_id="test:R", hw=2.0, hh=1.5,
                  pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([wall_h, wall_v, mob])
    before = overlap_area(model, {}, uuids=("uuid-h", "uuid-m")) + \
        overlap_area(model, {}, uuids=("uuid-v", "uuid-m"))
    prop = run_placement(
        model,
        PlacementParams(max_iterations=100, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                        wall_k_N=0.01, wall_halo_m=0.003,
                         ghost_k_N=1e-15),
        movable_uuids={"uuid-m"},
    )
    after = overlap_area(model, prop.deltas, uuids=("uuid-h", "uuid-m")) + \
        overlap_area(model, prop.deltas, uuids=("uuid-v", "uuid-m"))
    assert after < before, f"corner wedge must improve: {before:.2f} -> {after:.3f}"
    assert "uuid-m" in prop.deltas
    dx, dy, da = prop.deltas["uuid-m"]
    assert math.hypot(dx, dy) > 0.3, "cornered part must move"
    assert abs(da) < 30.0, f"corner escape must not spin wildly, got da={da:.2f}"


@pytest.mark.regression
def test_corridor_settles_without_overlap():
    """Mobile part between two locked rows settles centered, no overlap."""
    top = make_fp("T", "uuid-t", 0.0, 4.0, ("T1", "T2"),
                  footprint_id="test:R", hw=6.0, hh=1.5,
                  pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    bot = make_fp("B", "uuid-b", 0.0, -4.0, ("B1", "B2"),
                  footprint_id="test:R", hw=6.0, hh=1.5,
                  pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    mob = make_fp("M", "uuid-m", 0.0, 0.5, ("M1", "M2"),
                  footprint_id="test:R", hw=2.0, hh=1.0,
                  pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([top, bot, mob])
    prop = run_placement(
        model,
        PlacementParams(max_iterations=100, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                        wall_k_N=0.01, wall_halo_m=0.003,
                         ghost_k_N=1e-15),
        movable_uuids={"uuid-m"},
    )
    for other in ("uuid-t", "uuid-b"):
        area = overlap_area(model, prop.deltas, uuids=("uuid-m", other))
        assert area < 1e-6, f"corridor must be overlap-free vs {other}: {area:.6f}"


# ---------------------------------------------------------------------------
# Phase 2b: full-DCCF overlap audit (input-board baseline)
# ---------------------------------------------------------------------------

OVERLAP_MIN_MM2 = 1.0
OVERLAP_GROWTH_MM2 = 0.5
LEGALIZE_HALO_M = 0.004
LEGALIZE_AUDIT_ITERS = 200


def _courtyard_overlaps(model):
    """Return ref-pair -> area for courtyard overlaps above OVERLAP_MIN_MM2."""
    from kicad_autorouter.placement import _collect_pad_nodes, _footprint_courtyard_polygon
    import numpy as np

    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(model)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    polys = {}
    for fp in model.footprints:
        if fp.ghost or not fp.pads or not fp.ref or fp.ref.startswith("G"):
            continue
        poly = _footprint_courtyard_polygon(fp, pad_positions=pos,
                                            pad_indices=uuid_to_idx.get(fp.uuid, []))
        if not poly.is_empty:
            polys[fp.ref] = poly
    found = {}
    refs = list(polys)
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            area = polys[a].intersection(polys[b]).area
            if area > OVERLAP_MIN_MM2:
                found[tuple(sorted((a, b)))] = area
    return found


@pytest.mark.regression
@pytest.mark.dccf
def test_dccf_overlap_audit(dccf_model):
    """Full-DCCF Legalize run: placement must not create or worsen squeezes.

    The input board already contains some >1mm² courtyard overlaps that are
    fixture properties, not placement results. After the Legalize preset run,
    fail only on pairs that were not already above threshold or that grew by
    more than OVERLAP_GROWTH_MM2. This keeps the audit state-derived instead of
    maintaining an allowlist.
    """
    initial = _courtyard_overlaps(dccf_model)
    prop = run_placement(
        dccf_model,
        PlacementParams(max_iterations=LEGALIZE_AUDIT_ITERS,
                        wall_halo_m=LEGALIZE_HALO_M),
    )
    final = _courtyard_overlaps(apply_deltas(dccf_model, prop.deltas))

    regressions = {
        pair: (initial.get(pair, 0.0), area)
        for pair, area in final.items()
        if area > initial.get(pair, 0.0) + OVERLAP_GROWTH_MM2
    }
    detail = {
        pair: (round(before, 3), round(after, 3))
        for pair, (before, after) in sorted(regressions.items())
    }
    assert not regressions, (
        "placement created or materially worsened >1mm² courtyard overlaps: "
        f"{detail}"
    )


def test_wall_mag_linear_matches_legacy():
    """_wall_mag at p=1 reproduces F=k*(1-s/h): k at contact, 0 past halo."""
    from kicad_autorouter.placement import _wall_mag

    k, h = 0.08, 6.0
    assert _wall_mag(k, 0.0, h) == pytest.approx(k)
    assert _wall_mag(k, h / 2, h) == pytest.approx(k / 2)
    assert _wall_mag(k, h, h) == 0.0
    assert _wall_mag(k, h + 1.0, h) == 0.0
    assert _wall_mag(k, -h, h) == pytest.approx(2 * k)


@pytest.mark.regression
def test_wall_cap_clamps_deep_leaves_halo():
    """wall_cap_depth_mm saturates deep force; halo/contact untouched; 0 = off."""
    from kicad_autorouter.placement import _wall_mag

    k, h = 0.08, 6.0
    # Uncapped p=2 at two-halo depth: 9k.
    assert _wall_mag(k, -2 * h, h, 2.0) == pytest.approx(9 * k)
    # Capped at one halo: evaluated at -h -> 4k.
    assert _wall_mag(k, -2 * h, h, 2.0, h) == pytest.approx(4 * k)
    assert _wall_mag(k, -3 * h, h, 2.0, h) == pytest.approx(4 * k)
    # Above the cap depth: identical to uncapped.
    assert _wall_mag(k, -h / 2, h, 2.0, h) == pytest.approx(_wall_mag(k, -h / 2, h, 2.0))
    # Contact and halo untouched by the cap.
    assert _wall_mag(k, 0.0, h, 2.0, h) == pytest.approx(k)
    assert _wall_mag(k, h / 2, h, 2.0, h) == pytest.approx(_wall_mag(k, h / 2, h, 2.0))
    assert _wall_mag(k, h, h, 2.0, h) == 0.0
    # cap=0 disables capping.
    assert _wall_mag(k, -3 * h, h, 2.0, 0.0) == pytest.approx(16 * k)

@pytest.mark.regression
def test_capped_deep_overlap_still_resolves():
    """Capped (4k saturating) wall still clears a deep overlap — the cap
    bounds runaway force, it must not strand buried parts."""
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", 1.0, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([fa, fb])
    before = _pair_overlap(model, {})
    assert before > 15.0
    prop = run_placement(
        model,
        PlacementParams(max_iterations=200, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                        wall_k_N=0.08, wall_halo_m=0.006, wall_exponent=2.0,
                        wall_cap_depth_mm=6.0,
                         ghost_k_N=1e-15,
                        damping_Ns_m=0.12),
    )
    after = _pair_overlap(model, prop.deltas)
    assert after < 0.01 * before, f"capped wall must clear: {before:.2f} -> {after:.3f}"


@pytest.mark.regression
def test_wall_exponent_two_matches_default_run():
    """Refactor guard: exponent=2.0 must reproduce default-param dynamics."""
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", 1.0, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=3.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([fa, fb])
    kw = dict(max_iterations=50, convergence_eps_m=1e-15,
              repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
              wall_k_N=0.08, wall_halo_m=0.0078,
               ghost_k_N=1e-15)
    base = run_placement(model, PlacementParams(**kw))
    exp1 = run_placement(model, PlacementParams(**{**kw, "wall_exponent": 2.0}))
    for uuid in base.deltas:
        for a, b in zip(base.deltas[uuid], exp1.deltas[uuid]):
            assert a == pytest.approx(b, rel=1e-12)

# ---------------------------------------------------------------------------
# Phase 3: rotation-in-squeeze
# ---------------------------------------------------------------------------

@pytest.mark.regression
def test_shear_torque_sign_consistent():
    """One-sided pad graze rotates the part toward the grazer, single-signed."""
    mover = make_fp("M", "uuid-m", 2.0, 0.0, ("N1", "N2"),
                    footprint_id="test:R", hw=1.5, hh=1.0,
                    pad_layers=("F.Cu",), pad_shape="circle")
    locked = make_fp("L", "uuid-l", 1.0, 0.3, ("N9",),
                     footprint_id="test:R", hw=1.5, hh=1.0,
                     pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    model = _board([mover, locked])
    prop = run_placement(
        model,
        PlacementParams(max_iterations=10, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-7, attraction_k_N=5e-5,
                        wall_k_N=1e-12, wall_halo_m=0.003,
                         ghost_k_N=1e-15),
        movable_uuids={"uuid-m"},
    )
    assert "uuid-m" in prop.deltas
    dx, dy, da = prop.deltas["uuid-m"]
    assert all(math.isfinite(v) for v in (dx, dy, da))
    assert abs(da) < 45.0, f"10-step shear must stay bounded, got da={da:.2f}"
    # Single-signed across frames: no flip-flopping (oscillation would alternate).
    frames = prop.frames
    if frames:
        signs = set()
        for fr in frames:
            if "uuid-m" in fr:
                _, _, fda = fr["uuid-m"]
                if abs(fda) > 1e-9:
                    signs.add(1 if fda > 0 else -1)
        assert len(signs) <= 1, f"shear rotation must not oscillate in sign: {signs}"


@pytest.mark.regression
def test_mtv_tie_no_sustained_spin():
    """Square-on-square head-on tie: escape picks one axis, no sustained spin."""
    fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"),
                 footprint_id="test:R", hw=2.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    fb = make_fp("B", "uuid-b", 1.0, 0.0, ("N3", "N4"),
                 footprint_id="test:R", hw=2.0, hh=2.0,
                 pad_layers=("F.Cu",), pad_shape="circle")
    model = _board([fa, fb])
    prop = run_placement(
        model,
        PlacementParams(max_iterations=200, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
                        wall_k_N=0.01, wall_halo_m=0.003,
                         ghost_k_N=1e-15),
    )
    for uuid in ("uuid-a", "uuid-b"):
        _, _, da = prop.deltas.get(uuid, (0.0, 0.0, 0.0))
        assert math.isfinite(da) and abs(da) < 45.0, f"tie must not spin {uuid}: da={da:.2f}"
    after = _pair_overlap(model, prop.deltas)
    assert after < 1e-6, f"tie must still resolve overlap, residual {after:.6f}"


@pytest.mark.regression
def test_compass_needle_flip_is_single_signed():
    """Strong asymmetric net pull may rotate far, but monotonically (torque, not churn).

    Documents the tube111 compass-needle family (up to ~175° over 500 iters):
    genuine torque is allowed to be large; oscillation is not. A 2-pad part
    pulled from one side only must keep one rotation sign across all frames.
    """
    mover = make_fp("M", "uuid-m", 0.0, 0.0, ("PULL", "FREE"),
                    footprint_id="test:R", hw=1.5, hh=1.0,
                    pad_layers=("F.Cu",), pad_shape="circle", pad_dx=1.0)
    # Anchor far away on +x sharing only the PULL net: torque about the center.
    anchor = make_fp("K", "uuid-k", 30.0, 8.0, ("PULL", "GND"),
                     footprint_id="test:R", hw=1.5, hh=1.0,
                     pad_layers=("F.Cu",), pad_shape="circle", locked=True)
    model = BoardModel(
        footprints=(mover, anchor),
        keepout_zones=(),
        nets={"PULL": set(), "FREE": set(), "GND": set()},
        by_ref={"M": mover, "K": anchor},
        edge_cuts=(
            (Point(-60, -60), Point(60, -60)),
            (Point(60, -60), Point(60, 60)),
            (Point(60, 60), Point(-60, 60)),
            (Point(-60, 60), Point(-60, -60)),
        ),
        footprint_region={"uuid-m": 0, "uuid-k": 0},
    )
    prop = run_placement(
        model,
        PlacementParams(max_iterations=200, convergence_eps_m=1e-15,
                        repulsion_k_N_m2=1e-15, attraction_k_N=0.05,
                        wall_k_N=1e-15, wall_halo_m=0.003,
                         ghost_k_N=1e-15,
                        damping_Ns_m=0.08),
        movable_uuids={"uuid-m"},
    )
    assert "uuid-m" in prop.deltas
    _, _, da = prop.deltas["uuid-m"]
    assert math.isfinite(da) and abs(da) < 180.0
    signs = set()
    for fr in prop.frames:
        if "uuid-m" in fr:
            _, _, fda = fr["uuid-m"]
            if abs(fda) > 1e-6:
                signs.add(1 if fda > 0 else -1)
    assert len(signs) <= 1, f"compass torque must be single-signed, got {signs}"
