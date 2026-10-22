#!/usr/bin/env python3
"""Generate before/after visualization PCBs for failing placement tests.

Saves to ./tmp/visualizations/failures/ for manual review.
"""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path

# Ensure project root on path (contains tests/ and src/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from kicad_autorouter.board_model import BoardModel, Point, Footprint, NetConnection, apply_deltas
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.sexpr import parse_file, to_sexpr
from kicad_autorouter.svg_render import render_board_svg
from kicad_autorouter.io import nudge_footprint_by_uuid
from tests.helpers import make_fp, make_rect_courtyard

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "tmp" / "visualizations" / "failures"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DCCF_FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "DCCF.sved.kicad_pcb"


def load_dccf_model():
    """Load DCCF board model."""
    from kicad_autorouter.board_model import board_model
    return board_model(parse_file(DCCF_FIXTURE))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _board(fps, edge=100.0):
    """Create a board model with large edge cuts."""
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


def _base_params(**kw):
    base = dict(
        max_iterations=50,
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05,
        wall_k_N=0.002,
        wall_halo_m=0.003,
        wall_exponent=1.0,
        momentum_alpha=0.0,
    )
    base.update(kw)
    return PlacementParams(**base)


def _production_params(**kw):
    """DCCF legalize-regime params."""
    base = dict(
        repulsion_k_N_m2=2e-9,
        attraction_k_N=8e-3,
        wall_k_N=8e-2,
        wall_halo_m=0.006,
        max_iterations=500,
    )
    base.update(kw)
    return PlacementParams(**base)


def _pair_board(fa, fb):
    """Two-footprint board for isolated pair probes."""
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


def _board_from_model(model):
    """Reconstruct PCB tree from model (for saving)."""
    pass


# ---------------------------------------------------------------------------
# Test Case Definitions (using test.helpers.make_fp signature)
# ---------------------------------------------------------------------------

def make_test_mobile_surrounded():
    """Test 1: test_mobile_part_surrounded_by_multiple_locked"""
    def _make_locked_neighbors(cx, cy, radius, n):
        locked = []
        for k in range(n):
            ang = 2 * math.pi * k / n
            x = cx + radius * math.cos(ang)
            y = cy + radius * math.sin(ang)
            locked.append(
                make_fp(
                    ref=f"LOCKED{k}", uuid=f"uuid-locked{k}", x=x, y=y,
                    nets=("L1", "L2"),
                    footprint_id="test:R", hw=1.5, hh=1.0,
                    pad_layers=("F.Cu",), pad_shape="circle", locked=True,
                )
            )
        return locked

    locked = _make_locked_neighbors(0.0, 0.0, 4.0, 6)
    mobile = make_fp(
        ref="MOBILE", uuid="uuid-mobile", x=0.0, y=0.0,
        nets=("NET1", "NET2"),
        footprint_id="test:R", hw=1.5, hh=1.0,
        pad_layers=("F.Cu",), pad_shape="circle", locked=False,
    )
    fps = tuple(locked) + (mobile,)
    model = _board(fps)
    params = PlacementParams(
        max_iterations=50, convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05, wall_k_N=0.01, wall_halo_m=0.003,
    )
    movable = {"uuid-mobile"}
    return model, params, movable


def make_test_mobile_between_two_locked():
    """Test 2: test_mobile_between_two_locked_linear"""
    left = make_fp(
        ref="LEFT", uuid="uuid-left", x=-4.0, y=0.0, locked=True,
        nets=("L1", "L2"),
        footprint_id="test:R", hw=3.0, hh=2.0,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    right = make_fp(
        ref="RIGHT", uuid="uuid-right", x=4.0, y=0.0, locked=True,
        nets=("R1", "R2"),
        footprint_id="test:R", hw=3.0, hh=2.0,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    mobile = make_fp(
        ref="MID", uuid="uuid-mid", x=1.0, y=0.0, locked=False,
        nets=("M1", "M2"),
        footprint_id="test:R", hw=2.5, hh=1.5,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    model = _board((left, right, mobile))
    params = PlacementParams(
        max_iterations=50, convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05, wall_k_N=0.01, wall_halo_m=0.003,
    )
    return model, params, {"uuid-mid"}


def make_test_three_part_linear_squeeze():
    """Test 3: test_three_part_linear_squeeze"""
    left = make_fp(
        ref="L", uuid="uuid-l", x=-4.0, y=0.0, locked=True,
        nets=("L1", "L2"),
        footprint_id="test:R", hw=3.0, hh=2.0,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    right = make_fp(
        ref="R", uuid="uuid-r", x=4.0, y=0.0, locked=True,
        nets=("R1", "R2"),
        footprint_id="test:R", hw=3.0, hh=2.0,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    mid = make_fp(
        ref="M", uuid="uuid-m", x=1.0, y=0.0, locked=False,
        nets=("M1", "M2"),
        footprint_id="test:R", hw=2.5, hh=1.5,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    model = _board((left, right, mid))
    params = PlacementParams(
        max_iterations=200, convergence_eps_m=1e-15,
        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
        wall_k_N=0.01, wall_halo_m=0.003, ghost_k_N=1e-15,
    )
    return model, params, {"uuid-m"}


def make_test_symmetric_squeeze_no_spin():
    """Test 4: test_symmetric_squeeze_no_spin"""
    left = make_fp(
        ref="L", uuid="uuid-l", x=-3.0, y=0.0, locked=True,
        nets=("L1", "L2"),
        footprint_id="test:R", hw=2.0, hh=2.0,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    right = make_fp(
        ref="R", uuid="uuid-r", x=3.0, y=0.0, locked=True,
        nets=("R1", "R2"),
        footprint_id="test:R", hw=2.0, hh=2.0,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    mid = make_fp(
        ref="M", uuid="uuid-m", x=0.0, y=0.0, locked=False,
        nets=("M1", "M2"),
        footprint_id="test:R", hw=2.0, hh=1.5,
        pad_layers=("F.Cu",), pad_shape="circle",
    )
    model = _board((left, right, mid))
    params = PlacementParams(
        max_iterations=50, convergence_eps_m=1e-15,
        repulsion_k_N_m2=1e-15, attraction_k_N=1e-15,
        wall_k_N=0.01, wall_halo_m=0.003, ghost_k_N=1e-15,
    )
    return model, params, {"uuid-m"}


def make_test_loaded_contact():
    """Test 5: test_loaded_contact_settles_despite_per_pad_loads"""
    from tests.helpers import make_fp as make_test_fp
    fa = make_test_fp(
        ref="A", uuid="uuid-a", x=2.0, y=0.0,
        nets=("N1", "NX"),
        footprint_id="test:R", hw=3.0, hh=2.0,
        pad_layers=("F.Cu",), pad_shape="circle", locked=False,
    )
    fb = make_test_fp(
        ref="B", uuid="uuid-b", x=10.0, y=0.0,
        nets=("N1", "NY"),
        footprint_id="test:R", hw=3.0, hh=2.0,
        pad_layers=("F.Cu",), pad_shape="circle", locked=True,
    )
    model = _board([fa, fb], edge=100.0)
    params = PlacementParams(
        max_iterations=500, repulsion_k_N_m2=1e-15, attraction_k_N=0.03,
        wall_k_N=0.05, wall_halo_m=0.006,
    )
    return model, params, None


def make_test_r5_legalize(dccf_model):
    """Test 6: test_r5_legalize_chatter_stops_stalled"""
    r5 = dccf_model.by_ref["R5"]
    params = PlacementParams(
        max_iterations=1000, attraction_k_N=0.008, wall_k_N=0.09,
        damping_Ns_m=0.13, dt_s=1e-3, vmax_m_s=1.0, friction_N=1e-6,
    )
    return dccf_model, params, {r5.uuid}


def make_test_dccf_multiple_resistors(dccf_model):
    """Test 7: test_dccf_multiple_resistors_near_locked"""
    mobile_refs = ["R11", "R3", "R6", "R10", "R12"]
    mobile_uuids = set()
    for ref in mobile_refs:
        fp = dccf_model.by_ref.get(ref)
        if fp and not fp.locked:
            mobile_uuids.add(fp.uuid)
    params = PlacementParams(
        max_iterations=50, convergence_eps_m=1e-12,
        repulsion_k_N_m2=2e-9, attraction_k_N=5e-05,
        wall_k_N=0.01, wall_halo_m=0.003, wall_exponent=1.0,
        momentum_alpha=0.0,
    )
    return dccf_model, params, mobile_uuids


def make_test_dccf_r13_rv1(dccf_model):
    """Test 8: test_dccf_r13_rv1_edge_contact (full board)"""
    r13 = dccf_model.by_ref["R13"]
    params = _production_params(max_iterations=500, convergence_eps_m=1e-9)
    return dccf_model, params, {r13.uuid}


def make_test_dccf_c14_rv3_u3(dccf_model):
    """Test 9: test_dccf_resistor_c14_near_rv3_u3 (full board)"""
    c14 = dccf_model.by_ref.get("C14")
    params = _production_params(max_iterations=2000, convergence_eps_m=1e-12)
    return dccf_model, params, {c14.uuid}


def make_test_dccf_overlap_audit(dccf_model):
    """Test 10: test_dccf_overlap_audit"""
    return dccf_model, PlacementParams(), None


# ---------------------------------------------------------------------------
# Visualization Functions
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "tmp" / "visualizations" / "failures"

def save_before_after(model, params, movable_uuids, test_name, dccf_model=None):
    """Run placement and save before/after PCBs + SVGs."""
    test_dir = OUTPUT_DIR / test_name
    test_dir.mkdir(parents=True, exist_ok=True)

    # Get original PCB tree
    tree = None
    if dccf_model is not None:
        tree = parse_file(DCCF_FIXTURE)

    # Save before state (SVG)
    before_svg = test_dir / f"{test_name}_before.svg"
    before_svg_content = render_board_svg(model, title=f"{test_name} - BEFORE")
    before_svg.write_text(before_svg_content, encoding="utf-8")

    # Run placement
    if dccf_model is not None:
        # For fixture tests, we may need specific movable_uuids
        prop = run_placement(model, params, movable_uuids=movable_uuids)
    else:
        prop = run_placement(model, params)

    # Apply deltas to model for after state
    model_after = apply_deltas(model, prop.deltas)

    # Save after state (SVG)
    after_svg = test_dir / f"{test_name}_after.svg"
    proposal = None
    if prop.deltas:
        from kicad_autorouter.placement import PlacementProposal
        proposal = PlacementProposal(
            deltas=prop.deltas, iterations=prop.iterations,
            final_max_disp_mm=prop.final_max_disp_mm,
            elapsed_s=prop.elapsed_s, params={},
        )
    after_svg_content = render_board_svg(model_after, proposal=proposal,
                                          title=f"{test_name} - AFTER")
    after_svg.write_text(after_svg_content, encoding="utf-8")

    # Also save PCBs for DCCF tests
    if tree is not None:
        from kicad_autorouter.io import nudge_footprint_by_uuid
        tree_after = tree
        for uuid_, delta in prop.deltas.items():
            dx, dy, da = delta[0], delta[1], delta[2]
            tree_after = nudge_footprint_by_uuid(tree_after, uuid_, dx, dy, da)
        after_pcb = test_dir / f"{test_name}_after.kicad_pcb"
        after_pcb.write_text(to_sexpr(tree_after) + "\n", encoding="utf-8")
        before_pcb = test_dir / f"{test_name}_before.kicad_pcb"
        before_pcb.write_text(to_sexpr(tree) + "\n", encoding="utf-8")

    # Save diagnostic info
    info_file = test_dir / f"{test_name}_info.txt"
    with info_file.open("w") as f:
        f.write(f"Test: {test_name}\n")
        f.write(f"Stop reason: {prop.stop_reason}\n")
        f.write(f"Iterations: {prop.iterations}\n")
        f.write(f"Max displacement: {prop.final_max_disp_mm:.3f} mm\n")
        f.write(f"Stop reason: {prop.stop_reason}\n")
        f.write(f"Residual max force: {prop.residual_max_force_N:.6f} N\n")
        f.write(f"Legality: {prop.legality_is_legal}\n")
        f.write(f"Overlapping pairs: {len(prop.overlapping_pairs)}\n")
        f.write(f"Max pair overlap: {prop.max_pair_overlap_mm2:.3f} mm²\n")
        f.write(f"Max wall penetration: {prop.max_wall_penetration_mm:.3f} mm\n")
        for a, b, area in prop.overlapping_pairs:
            f.write(f"  Overlap: {a} / {b} = {area:.3f} mm²\n")
        for fp_uuid, (dx, dy, da) in prop.deltas.items():
            fp = next((fp for fp in model.footprints if fp.uuid == fp_uuid), None)
            if fp:
                f.write(f"  {fp.ref or fp_uuid[:8]}: dx={dx:.3f} dy={dy:.3f} da={da:.3f}\n")

    print(f"  Saved {test_name}: {before_svg}, {after_svg}" + (", PCBs" if tree else ""))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "tmp" / "visualizations" / "failures"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DCCF_FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "DCCF.sved.kicad_pcb"


def _base_params(**kw):
    base = dict(
        max_iterations=50,
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05,
        wall_k_N=0.002,
        wall_halo_m=0.003,
        wall_exponent=1.0,
        momentum_alpha=0.0,
    )
    base.update(kw)
    return PlacementParams(**base)


def _production_params(**kw):
    """DCCF legalize-regime params."""
    base = dict(
        repulsion_k_N_m2=2e-9,
        attraction_k_N=8e-3,
        wall_k_N=8e-2,
        wall_halo_m=0.006,
        max_iterations=500,
    )
    base.update(kw)
    return PlacementParams(**base)


def _pair_board(fa, fb):
    """Two-footprint board for isolated pair probes."""
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Output directory: {OUTPUT_DIR}")

    # Load DCCF model once for fixture tests
    dccf_model = load_dccf_model()

    tests = [
        ("test_mobile_part_surrounded_by_multiple_locked", make_test_mobile_surrounded, None),
        ("test_mobile_between_two_locked_linear", make_test_mobile_between_two_locked, None),
        ("test_three_part_linear_squeeze", make_test_three_part_linear_squeeze, None),
        ("test_symmetric_squeeze_no_spin", make_test_symmetric_squeeze_no_spin, None),
        ("test_loaded_contact_settles_despite_per_pad_loads", make_test_loaded_contact, None),
        ("test_r5_legalize_chatter_stops_stalled", make_test_r5_legalize, None),
        ("test_dccf_multiple_resistors_near_locked", make_test_dccf_multiple_resistors, None),
        ("test_dccf_r13_rv1_edge_contact", make_test_dccf_r13_rv1, None),
        ("test_dccf_resistor_c14_near_rv3_u3", make_test_dccf_c14_rv3_u3, None),
        ("test_dccf_overlap_audit", make_test_dccf_overlap_audit, None),
    ]

    for name, maker, _ in tests:
        try:
            print(f"\n=== {name} ===")
            if name.startswith("test_dccf_") or name.startswith("test_r5_"):
                model, params, movable = maker(dccf_model)
            else:
                model, params, movable = maker()
            save_before_after(model, params, movable, name)
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()

    print(f"\nDone! Output in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()