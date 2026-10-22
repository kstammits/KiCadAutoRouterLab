"""Movable-vs-immobile courtyard pairing + board-wall containment.

Regression tests for the 2026-10-10 R11-into-RV2 incident on DCCF:

1. The courtyard pair loop was ``for j in range(i + 1, ...)`` under
   ``if not movable[i]: continue``, so a movable footprint never felt an
   immobile (locked/unselected) footprint sorting earlier in file order —
   a lone selected part collided with only ~half the board.
2. The board wall (region polygon as inverted courtyard, same wall law)
   pushes near-edge parts inside and pulls fully-outside parts home.

Board builders mirror tests/test_placement_layers.py (valid head-to-tail
courtyard rects, so these tests isolate the pairing/wall logic from
the known courtyard-shape debt noted in TODO.md).
"""

import math

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    Point,
    apply_deltas,
)
from kicad_autorouter.placement import (
    PlacementParams,
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
    convergence_eps_m=1e-15,
    repulsion_k_N_m2=1.0000000000000001e-07,
    attraction_k_N=5e-05,
    wall_k_N=0.01,
    wall_halo_m=0.003,
      # negligible: parts start at board center
)


SEPARATION_PARAMS = PlacementParams(
    # Short horizon on purpose: a few iterations already carry the full
    # signal (pair evaluated, ~1e5 force, ~mm motion), while the
    # long-horizon stiff-contact regime oscillates and would make the
    # test about integrator dynamics instead of pairing. Thresholds
    # calibrated by running: old code reports EXACTLY 0.0 / no motion.
    max_iterations=3,
    convergence_eps_m=1e-15,
    repulsion_k_N_m2=1.0000000000000001e-07,
    attraction_k_N=5e-05,
    wall_k_N=0.01,
    wall_halo_m=0.003,
    
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
    assert set(comps) == {"repulsion", "attraction", "ghost", "courtyard", "tangential"}
    # NOTE (SI MTV contact): the 5mm-x / 4mm-y overlap escapes along the
    # minimum axis (±y), so the wall force reads on cy, not cx. Old code
    # reported exactly 0.0 on every axis (pair never evaluated).
    _, cy = comps["courtyard"]
    assert cy > 1e-3


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
    # NOTE (SI MTV contact): escape axis is ±y here; see above.
    _, cy = prop.force_components["uuid-b"]["courtyard"]
    assert cy < -1e-3  # old code: exactly 0.0 (pair never evaluated)


@pytest.mark.regression
def test_shear_produces_finite_rotation():
    """Rotational shear is governed by rigid projection + contact weighting.

    Replaces test_rotation_rate_capped_per_iteration (2026-10-11): the
    max_dangle_deg cap is removed -- there is no velocity to damp, only
    per-iteration displacements. A locked part sits ~0.3mm from one pad
    of a 2-pad movable part, so per-pad repulsion differs by ~50x across
    the footprint. Courtyard kc is near-zero on purpose to isolate
    repulsion shear. What this guards: the shear resolves to finite
    motion (no NaN, no full spins in a single step from a sub-mm
    layout), with rotation read out via Kabsch as before.
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
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05,
        wall_k_N=1e-12,  # near-zero: isolate repulsion shear
        wall_halo_m=0.003,
        
    )
    prop = run_placement(model, PlacementParams(**base), movable_uuids={"uuid-m"})
    assert "uuid-m" in prop.deltas
    import math

    dx, dy, da = prop.deltas["uuid-m"]
    assert all(math.isfinite(v) for v in (dx, dy, da))
    assert abs(da) < 180.0, f"single step must not spin: da={da}"


@pytest.mark.regression
def test_contact_weighting_conserves_total():
    """F4: the contact-weighted spread conserves the footprint total.

    B (2 pads) and a single-pad probe S share the identical courtyard
    layout relative to locked A, so S must feel exactly the SAME total
    courtyard force as B (weights sum to 1 by construction, distributing
    the pair force across pads while preserving the total). Repulsion
    differs by pad count, so only courtyard components are compared.
    """
    from .helpers import make_fp as _make_fp

    model = _board(first_locked=True)
    params = PlacementParams(
        max_iterations=0,
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05,
        wall_k_N=0.01,
        wall_halo_m=0.003,
        
    )
    prop = run_placement(model, params, movable_uuids={"uuid-b"})
    total_fx, total_fy = prop.forces["uuid-b"]
    # SI newtons: Coulomb ~0.1N in x, wall ~0.06N in y (MTV escape axis).
    assert math.hypot(total_fx, total_fy) > 1e-3  # sanity: the pair push exists
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
    assert cy_s == pytest.approx(cy_b, rel=1e-9), (
        f"weighting changed the total: single={cy_s}, B={cy_b}"
    )


@pytest.mark.regression
def test_contact_weights_unit():
    """_contact_weights: contact side loaded, sum exactly 1, single -> 1."""
    import numpy as np

    from kicad_autorouter.placement import _contact_weights

    w = _contact_weights(np.array([-0.5, 0.5]))
    assert w[0] > w[1]  # contact side (negative proj) takes more
    assert sum(w) == pytest.approx(1.0)  # sum exactly 1: total conserved
    assert _contact_weights(np.array([0.0])) == pytest.approx([1.0])
    assert sum(_contact_weights(np.array([-0.9, -0.1, 0.4]))) == pytest.approx(1.0)


@pytest.mark.regression
def test_boundary_force_bounded_near_edge():
    """Integration: a 60x40mm part starting near the left edge on a
    wide board. The boundary force should push it right (positive x) when
    within the halo zone. The force per pad is bounded by wall_k_N.
    """
    # Place part with courtyard left edge at -38.0 (2mm from board edge at -40).
    # Courtyard center = left_edge + hw = -38.0 + 30.0 = -8.0
    part = _fp("P", "uuid-p", -8.0, False, "N1", "N2", hw=30.0, hh=20.0)
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
        board_regions=(
            BoardRegion(
                uuid="region-0",
                polygon=tuple(Point(x, y) for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]),
                bbox=(x0, x1, y0, y1),
            ),
        ),
    )
    params = PlacementParams(
        max_iterations=1,
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05,
        wall_k_N=0.5,
        wall_halo_m=0.006,
    )
    prop = run_placement(model, params)
    # Boundary force is now part of courtyard component
    bx, by = prop.force_components["uuid-p"]["courtyard"]
    assert math.isfinite(bx) and math.isfinite(by)
    assert by == 0.0  # far from top/bottom edges
    # Part is 2mm from left edge, halo = 6mm, so force is active.
    # Force should be positive (pushing right, away from edge).
    assert bx > 0.0
    # Force magnitude should not exceed wall_k_N per pad (saturated at boundary)
    # With 2 pads, total force <= 2 * wall_k_N
    assert bx <= 2.0 * params.wall_k_N * len(part.pads)


# ---------------------------------------------------------------------------
# Multiple locked neighbors scenarios (DCCF-style)
# ---------------------------------------------------------------------------

def _make_locked_neighbors(center_x: float, center_y: float, radius: float, count: int):
    """Create locked parts in a circle around a center point."""
    import math
    locked_parts = []
    for i in range(count):
        angle = 2 * math.pi * i / count
        x = center_x + radius * math.cos(angle)
        y = center_y + radius * math.sin(angle)
        locked_parts.append(
            _fp(f"LOCKED{i}", f"uuid-locked-{i}", x, True, f"L{i}A", f"L{i}B")
        )
    return locked_parts


@pytest.mark.regression
def test_mobile_part_surrounded_by_multiple_locked():
    """One mobile part centered among 6 locked parts: it will stay trapped.
    """
    locked = _make_locked_neighbors(0.0, 0.0, 4.0, 6)  # 6 locked parts in circle radius 4mm
    mobile = _fp("MOBILE", "uuid-mobile", 0.0, False, "NET1", "NET2", hw=1.5, hh=1.0)
    # Place mobile exactly at center - heavy overlap with all 6 locked parts
    fps = tuple(locked) + (mobile,)
    model = BoardModel(
        footprints=fps,
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-30, -30), Point(30, -30)),
            (Point(30, -30), Point(30, 30)),
            (Point(30, 30), Point(-30, 30)),
            (Point(-30, 30), Point(-30, -30)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )
    params = PlacementParams(
        max_iterations=200,
    )
    prop = run_placement(model, params, movable_uuids={"uuid-mobile"})
    assert "uuid-mobile" in prop.deltas
    dx, dy, _ = prop.deltas["uuid-mobile"]
    assert math.hypot(dx, dy) < 0.750, "Mobile part should stay in the cage"


@pytest.mark.regression
def test_mobile_between_two_locked_linear():
    """Mobile part between two locked parts on a line: should be pushed
    along the line to escape, not perpendicular.

    Mirrors DCCF C14 between RV3 and U3.
    """
    # Two locked parts on x-axis; the mobile part starts off-center (as in
    # the real DCCF C14/RV3/U3 wedge — a perfectly centered part is an
    # unstable equilibrium with exactly zero net force, correctly frozen).
    left = _fp("LEFT", "uuid-left", -4.0, True, "L1", "L2", hw=3.0, hh=2.0)
    right = _fp("RIGHT", "uuid-right", 4.0, True, "R1", "R2", hw=3.0, hh=2.0)
    # Mobile part nearer the right neighbor: deeper overlap on the right.
    mobile = _fp("MID", "uuid-mid", 1.0, False, "M1", "M2", hw=2.5, hh=1.5)
    
    fps = (left, right, mobile)
    model = BoardModel(
        footprints=fps,
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-30, -30), Point(30, -30)),
            (Point(30, -30), Point(30, 30)),
            (Point(30, 30), Point(-30, 30)),
            (Point(-30, 30), Point(-30, -30)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )
    # Use more iterations to escape
    params = PlacementParams(
        max_iterations=50,
    )
    prop = run_placement(model, params, movable_uuids={"uuid-mid"})
    
    assert "uuid-mid" in prop.deltas
    dx, dy, _ = prop.deltas["uuid-mid"]
    # Tangent force changes escape direction; part is ejected from the squeeze.
    assert math.hypot(dx, dy) > 1.0, f"part should escape, got dx={dx:.3f}, dy={dy:.3f}"


@pytest.mark.regression
def test_mobile_near_locked_cluster_with_attraction():
    """Mobile part attracted to a net on a locked part, but repelled by
    courtyard: should settle at courtyard edge near the pad.

    Mirrors DCCF resistors (R11, R13) attracted to RV1/RV3 pads but
    kept outside courtyards by repulsion.
    """
    # Large locked part (RV-like) with courtyard
    locked = _fp("RV", "uuid-rv", 0.0, True, "RV_PAD", "GND", hw=38.0, hh=5.0)
    # Mobile resistor sharing net with locked part's pad
    mobile = _fp("R", "uuid-r", -8.0, False, "RV_PAD", "GND", hw=1.5, hh=1.0)
    
    fps = (locked, mobile)
    model = BoardModel(
        footprints=fps,
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-60, -30), Point(60, -30)),
            (Point(60, -30), Point(60, 30)),
            (Point(60, 30), Point(-60, 30)),
            (Point(-60, 30), Point(-60, -30)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )
    params = PlacementParams(
        max_iterations=100,
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=0.01,  # Strong enough to pull toward pad
        wall_k_N=0.08,        # Strong enough to hold courtyard edge
        wall_halo_m=0.003,
        
    )
    prop = run_placement(model, params, movable_uuids={"uuid-r"})
    
    assert "uuid-r" in prop.deltas
    dx, dy, _ = prop.deltas["uuid-r"]
    final_x = -8.0 + dx
    final_y = 0.0 + dy
    
    # Should move toward locked part (positive x) but not enter courtyard
    # RV courtyard: x in [-1.25, 74.75], mobile half-width 1.5
    # So mobile center must be <= -2.75 to be outside
    assert final_x > -8.0, "Should move toward locked part's pad"
    assert final_x <= -2.75, f"Should not enter locked courtyard (x <= -2.75), got {final_x:.2f}"


# ---------------------------------------------------------------------------
# DCCF fixture integration tests
# ---------------------------------------------------------------------------

@pytest.mark.regression
@pytest.mark.dccf
def test_dccf_resistor_near_locked_rv_courtyards():
    """Test moving a DCCF resistor (R11) near locked RV parts.

    R11 sits between RV3 (locked) and U3 (mobile but treated as obstacle).
    This tests the specific migration bug where small parts wedged between
    locked neighbors get incorrect force aggregation.
    """
    from kicad_autorouter.board_model import board_model, apply_deltas
    from kicad_autorouter.sexpr import parse_file
    from pathlib import Path
    from kicad_autorouter.placement import _collect_pad_nodes, _footprint_courtyard_polygon
    import numpy as np

    model = board_model(parse_file(Path("tests/fixtures/DCCF.sved.kicad_pcb")))
    
    # R11 is mobile, near RV3 (locked) and U3 (mobile)
    r11 = model.by_ref.get("R11")
    rv3 = model.by_ref.get("RV3")
    u3 = model.by_ref.get("U3")
    
    assert r11 is not None and not r11.locked, "R11 should be mobile"
    assert rv3 is not None and rv3.locked, "RV3 should be locked"
    assert u3 is not None, "U3 should exist"
    
    # Run placement with only R11 movable
    params = PlacementParams(
        max_iterations=50,
    )
    prop = run_placement(model, params, movable_uuids={r11.uuid})
    
    # R11 should move
    assert r11.uuid in prop.deltas, "R11 should experience forces and move"
    dx, dy, _ = prop.deltas[r11.uuid]
    assert math.hypot(dx, dy) > 0.1, f"R11 should move significantly, got ({dx:.3f}, {dy:.3f})"
    
    # Check no courtyard overlap with RV3 after move
    moved = apply_deltas(model, prop.deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    
    r11_poly = _footprint_courtyard_polygon(
        moved.by_ref["R11"], pad_positions=pos, pad_indices=uuid_to_idx.get(r11.uuid, [])
    )
    rv3_poly = _footprint_courtyard_polygon(
        moved.by_ref["RV3"], pad_positions=pos, pad_indices=uuid_to_idx.get(rv3.uuid, [])
    )
    
    if not r11_poly.is_empty and not rv3_poly.is_empty:
        overlap = r11_poly.intersection(rv3_poly).area
        assert overlap <= 1e-6, f"R11/RV3 courtyard overlap after move: {overlap:.6f}mm²"


@pytest.mark.regression
@pytest.mark.dccf
def test_dccf_multiple_resistors_near_locked():
    """Test moving multiple DCCF resistors near locked RV/SW/D parts.

    Tests that multiple mobile parts near the same locked cluster
    don't interfere with each other's force calculation.
    """
    from kicad_autorouter.board_model import board_model, apply_deltas
    from kicad_autorouter.sexpr import parse_file
    from pathlib import Path
    from kicad_autorouter.placement import _collect_pad_nodes, _footprint_courtyard_polygon
    import numpy as np

    model = board_model(parse_file(Path("tests/fixtures/DCCF.sved.kicad_pcb")))

    # Pick several mobile resistors near the locked RV column.
    mobile_refs = ["R11", "R3", "R6", "R10", "R12"]
    mobile_uuids = set()
    for ref in mobile_refs:
        fp = model.by_ref.get(ref)
        if fp and not fp.locked:
            mobile_uuids.add(fp.uuid)

    assert len(mobile_uuids) >= 3, "Should have at least 3 mobile resistors"

    # Production-scale repulsion: 1e-7 is 50x the production default and rams
    # mobile parts through locked courtyards faster than the wall law can
    # reject them (deep-containment normal flip) — outside normal behavior.
    params = PlacementParams(
        max_iterations=50
    )
    prop = run_placement(model, params, movable_uuids=mobile_uuids)
    
    # All should move
    for uuid in mobile_uuids:
        assert uuid in prop.deltas, f"{uuid} should move"
    
    # Check no courtyard overlaps between any moved parts
    moved = apply_deltas(model, prop.deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    
    polys = {}
    for ref in ["RV1", "RV2", "RV3", "RV4", "RV5", "RV6"] + list(mobile_refs):
        fp = moved.by_ref.get(ref)
        if fp:
            polys[ref] = _footprint_courtyard_polygon(
                fp, pad_positions=pos, pad_indices=uuid_to_idx.get(fp.uuid, [])
            )
    
    refs = list(polys.keys())
    for i, a in enumerate(refs):
        for b in refs[i+1:]:
            pa, pb = polys[a], polys[b]
            if not pa.is_empty and not pb.is_empty:
                area = pa.intersection(pb).area
                assert area <= 1.0, f"{a}/{b} courtyard overlap: {area:.6f}mm²"


def _production_params(**kw):
    """DCCF legalize-regime params (engine defaults): the regime real boards run."""
    base = dict(repulsion_k_N_m2=2e-9, attraction_k_N=8e-3, wall_k_N=8e-2,
                wall_halo_m=0.006)
    base.update(kw)
    return PlacementParams(**base)


def _pair_board(fa, fb):
    """Two-footprint board for isolated pair probes (no nets: pure contact)."""
    edge = 200.0
    return BoardModel(
        footprints=(fa, fb),
        keepout_zones=(),
        nets={},
        by_ref={fa.ref: fa, fb.ref: fb},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={fa.uuid: 0, fb.uuid: 0},
    )


def _pair_overlap(model, deltas, ref_a, ref_b):
    from kicad_autorouter.placement import _collect_pad_nodes, _footprint_courtyard_polygon
    import numpy as np

    moved = apply_deltas(model, deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    pa = _footprint_courtyard_polygon(
        moved.by_ref[ref_a], pad_positions=pos,
        pad_indices=uuid_to_idx.get(moved.by_ref[ref_a].uuid, []))
    pb = _footprint_courtyard_polygon(
        moved.by_ref[ref_b], pad_positions=pos,
        pad_indices=uuid_to_idx.get(moved.by_ref[ref_b].uuid, []))
    if pa.is_empty or pb.is_empty:
        return 0.0
    return pa.intersection(pb).area


@pytest.mark.regression
@pytest.mark.dccf
def test_dccf_r13_rv1_edge_contact():
    """R13 vs locked RV1 at exact edge contact: the pair hold points AWAY.

    R13 starts exactly touching RV1's courtyard edge (R13 top y=71.10 =
    RV1 bottom y=71.10). The isolated pair probe must read the unified wall
    law at contact: F = -k straight down the MTV axis (no x-component, no
    pin-vote dilution). In the full board R13 settles into a documented
    squeeze wedge (southern halo field + net anchor across RV1 exceed the
    edge hold — a stable force balance, not a direction bug), so the
    full-model half pins convergence and boundedness instead of ejection:
    the endpoint must be stable across doubled budgets (no tunneling, no
    shuttle, no runaway).
    """
    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.sexpr import parse_file
    from pathlib import Path

    model = board_model(parse_file(Path("tests/fixtures/DCCF.sved.kicad_pcb")))
    r13 = model.by_ref["R13"]
    rv1 = model.by_ref["RV1"]
    assert r13 is not None and not r13.locked
    assert rv1 is not None and rv1.locked

    # Isolated pair: pure contact, unified law F = k*(1-s/h) at s ~= 0.
    pair = _pair_board(r13, rv1)
    prop = run_placement(pair, _production_params(max_iterations=0, convergence_eps_m=1e-15),
                         movable_uuids={r13.uuid})
    cx, cy = prop.force_components[r13.uuid]["courtyard"]
    assert cy == pytest.approx(-0.08, rel=0.2), f"edge hold must be -k, got cy={cy:.6f}"
    assert abs(cx) <= 0.005, f"hold must be axis-pure (-y), got cx={cx:.6f}"

    # Full board, only R13 movable: must converge to a stable endpoint.
    ends = []
    for budget in (500, 1000):
        prop = run_placement(model, _production_params(
            max_iterations=budget, convergence_eps_m=1e-9), movable_uuids={r13.uuid})
        dx, dy, _ = prop.deltas.get(r13.uuid, (0.0, 0.0, 0.0))
        ends.append((r13.x_mm + dx, r13.y_mm + dy))
        assert math.hypot(dx, dy) < 5.0, f"R13 must not tunnel, moved ({dx:.2f}, {dy:.2f})"
    assert math.hypot(ends[1][0] - ends[0][0], ends[1][1] - ends[0][1]) < 0.5, (
        f"R13 endpoint must be stable across doubled budgets: {ends}")
    assert _pair_overlap(model, prop.deltas, "R13", "RV1") < 40.0, (
        "R13/RV1 wedge is a documented stable residual; it must stay bounded, never run away")


@pytest.mark.regression
@pytest.mark.dccf
@pytest.mark.regression
@pytest.mark.dccf
def test_dccf_resistor_c14_near_rv3_u3():
    """C14 wedged between RV3 (locked) and U3: the RV3 escape axis pushes OUT.

    The motivating incident (old law: C14/RV3 2.84 -> 7.14mm² in 50 iters —
    the pair vote picked a centroid-diagonal axis and dove deeper). Under the
    unified law the isolated C14+RV3 probe reads +y (off RV3's top edge) and
    a single-movable production run clears the RV3 side 2.84 -> <0.5mm².
    """
    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.sexpr import parse_file
    from pathlib import Path

    model = board_model(parse_file(Path("tests/fixtures/DCCF.sved.kicad_pcb")))

    c14 = model.by_ref.get("C14")
    rv3 = model.by_ref.get("RV3")
    u3 = model.by_ref.get("U3")

    assert c14 is not None and not c14.locked, "C14 should be mobile"
    assert rv3 is not None and rv3.locked, "RV3 should be locked"
    assert u3 is not None, "U3 should exist"

    # Isolated pair: escape is +y (off RV3's top edge), axis-pure.
    pair = _pair_board(c14, rv3)
    prop = run_placement(pair, _production_params(max_iterations=0, convergence_eps_m=1e-15),
                         movable_uuids={c14.uuid})
    cx, cy = prop.force_components[c14.uuid]["courtyard"]
    assert cy > 0.05, f"C14/RV3 escape must point +y (out), got cy={cy:.6f}"
    assert abs(cx) <= 0.01, f"escape must be axis-pure (+y), got cx={cx:.6f}"

    # Full board, only C14 movable: RV3 side clears; endpoint converges.
    ends = []
    for budget in (500, 2000):
        prop = run_placement(model, _production_params(
            max_iterations=budget, convergence_eps_m=1e-12), movable_uuids={c14.uuid})
        assert c14.uuid in prop.deltas, "C14 should experience forces"
        dx, dy, _ = prop.deltas[c14.uuid]
        ends.append((c14.x_mm + dx, c14.y_mm + dy))
        assert math.hypot(dx, dy) < 5.0, f"C14 must not tunnel, moved ({dx:.2f}, {dy:.2f})"
    assert math.hypot(ends[1][0] - ends[0][0], ends[1][1] - ends[0][1]) < 0.5, (
        f"C14 endpoint must be stable across 4x budgets: {ends}")
    assert _pair_overlap(model, prop.deltas, "C14", "RV3") < 0.5, (
        "C14/RV3 must clear (was 7.14mm² under the old vote)")
