"""SI dynamics calibration sweep on the synthetic magnet board.

Why this exists: the SI rewrite (feature/si-dynamics) replaced the legacy
pseudo-force stepper with real m/N/s physics. The committed
``PlacementParams`` defaults are starting guesses from docs/si_force_laws.md,
not tuned values. A prior session burned tokens hand-tuning test constants
against those guesses.

This script sweeps a SMALL focused grid on the cheap synthetic magnet board
(7 resistors, shared nets NET_A/B/C) and reports which parameter sets make
the physics behave: groups tighten vs their own scattered start, parts stay
inside the board region, runs converge within budget.

Region-aware: the old test_magnet.py scatters parts in a hardcoded 100x100mm
square, but minimal.kicad_pcb's region is x 55-215 / y 45-145, so most parts
started OUTSIDE the board and the boundary force dominated. This harness
samples start poses inside the actual region bbox (inset by margin), with a
minimum center separation so the board starts overlap-free.

Usage:
    python3 scripts/calibrate_si.py --quick        # 24 combos x 3 seeds
    python3 scripts/calibrate_si.py                # 72 combos x 3 seeds
    python3 scripts/calibrate_si.py --out /tmp/si_sweep.json --top 10
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kicad_autorouter.board_model import (  # noqa: E402
    BoardModel,
    Footprint,
    Pad,
    Point,
    apply_deltas,
    board_model,
)
from kicad_autorouter.placement import (  # noqa: E402
    PlacementParams,
    _collect_pad_nodes,
    _footprint_courtyard_polygon,
    run_placement,
)
from kicad_autorouter.sexpr import parse_file  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"

COURTYARD_HALF_W = 1.5
COURTYARD_HALF_H = 1.0
PAD_OFFSET = 1.0
PAD_SIZE = (1.0, 1.0)
MARGIN_MM = 5.0  # inset from region bbox for start poses
MIN_SEPARATION_MM = 6.0  # min center distance at start (courtyard is 3x2)
MIN_LOCKED_CLEAR_MM = 10.0  # min center distance vs locked parts (MH courtyards)

GROUPS = {"A": ["R1", "R2", "R3"], "B": ["R4", "R5"], "C": ["R6", "R7"]}
REFS = ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]


def _make_courtyard():
    hw, hh = COURTYARD_HALF_W, COURTYARD_HALF_H
    corners = [
        Point(-hw, -hh),
        Point(hw, -hh),
        Point(hw, hh),
        Point(-hw, hh),
        Point(-hw, -hh),
    ]
    return tuple((corners[i], corners[i + 1]) for i in range(4))


def _create_resistor(ref, uuid, shared_net, unique_net, x_mm, y_mm):
    def to_board(px, py):
        return Point(x_mm + px, y_mm + py)

    pads = (
        Pad(
            number="1",
            net_name=shared_net,
            position=to_board(-PAD_OFFSET, 0.0),
            size_mm=PAD_SIZE,
            shape="rect",
            angle_deg=0.0,
            pad_type="smd",
            drill_mm=0.0,
            layers=("F.Cu", "F.Paste", "F.Mask"),
        ),
        Pad(
            number="2",
            net_name=unique_net,
            position=to_board(PAD_OFFSET, 0.0),
            size_mm=PAD_SIZE,
            shape="rect",
            angle_deg=0.0,
            pad_type="smd",
            drill_mm=0.0,
            layers=("F.Cu", "F.Paste", "F.Mask"),
        ),
    )
    return Footprint(
        ref=ref,
        footprint_id="Test:R_0805_Magnet",
        layer="F.Cu",
        x_mm=x_mm,
        y_mm=y_mm,
        angle_deg=0.0,
        pads=pads,
        courtyard=_make_courtyard(),
        locked=False,
        uuid=uuid,
        ghost=False,
        ghost_attractions=(),
    )


def _base_model():
    return board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))


def _region_bounds(base):
    min_x, max_x, min_y, max_y = base.board_regions[0].bbox
    return (
        min_x + MARGIN_MM,
        max_x - MARGIN_MM,
        min_y + MARGIN_MM,
        max_y - MARGIN_MM,
    )


def build_magnet_board(seed):
    """Overlap-free scattered start strictly inside the real board region."""
    base = _base_model()
    locked = tuple(
        Footprint(**{**fp.__dict__, "locked": True}) for fp in base.footprints
    )
    x0, x1, y0, y1 = _region_bounds(base)
    rng = random.Random(seed)
    specs = [
        ("R1", "mag-r1-uuid-0001", "NET_A", "NET_D"),
        ("R2", "mag-r2-uuid-0002", "NET_A", "NET_E"),
        ("R3", "mag-r3-uuid-0003", "NET_A", "NET_F"),
        ("R4", "mag-r4-uuid-0004", "NET_B", "NET_G"),
        ("R5", "mag-r5-uuid-0005", "NET_B", "NET_H"),
        ("R6", "mag-r6-uuid-0006", "NET_C", "NET_I"),
        ("R7", "mag-r7-uuid-0007", "NET_C", "NET_J"),
    ]
    # Keep clear of locked parts too (e.g. corner mounting holes): the old
    # harness only separated resistors from each other, so some starts
    # overlapped a locked MH courtyard and the wall force blasted light
    # parts into spins on iteration 1.
    locked_xy = [(fp.x_mm, fp.y_mm) for fp in locked]
    placed = []
    for ref, uid, shared, unique in specs:
        for _ in range(2000):
            x = rng.uniform(x0, x1)
            y = rng.uniform(y0, y1)
            if all(
                math.hypot(x - px, y - py) >= MIN_SEPARATION_MM
                for px, py in placed
            ) and all(
                math.hypot(x - lx, y - ly) >= MIN_LOCKED_CLEAR_MM
                for lx, ly in locked_xy
            ):
                break
        placed.append((x, y))
    resistors = tuple(
        _create_resistor(ref, uid, shared, unique, x, y)
        for (ref, uid, shared, unique), (x, y) in zip(specs, placed)
    )
    all_fps = locked + resistors
    return BoardModel(
        footprints=all_fps,
        board_regions=base.board_regions,
        footprint_region={fp.uuid: 0 for fp in all_fps if fp.uuid},
        by_ref={fp.ref: fp for fp in all_fps if fp.ref},
        nets=base.nets,
        zones=base.zones,
        tracks=base.tracks,
        vias=base.vias,
        keepout_zones=base.keepout_zones,
        ghost_footprints=(),
        edge_cuts=base.edge_cuts,
        edge_arcs=base.edge_arcs,
        version=base.version,
        version_warning=base.version_warning,
    )


def _pooled_intra(pos):
    ds = []
    for refs in GROUPS.values():
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                ds.append(math.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]))
    return sum(ds) / len(ds)


def _final_overlap_area_mm2(model, prop):
    """Total pairwise courtyard overlap at final pose (shapely, mm^2)."""
    moved = apply_deltas(model, prop.deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    polys = []
    for ref in REFS:
        fp = moved.by_ref[ref]
        polys.append(
            _footprint_courtyard_polygon(
                fp, pad_positions=pos, pad_indices=uuid_to_idx.get(fp.uuid, [])
            )
        )
    total = 0.0
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            try:
                total += polys[i].intersection(polys[j]).area
            except Exception:
                pass
    return total


def evaluate(params, seeds):
    """Run magnet board on seeds; return aggregate metrics (lower is better)."""
    tighten_ratios = []
    boundary_violations = 0
    overlap_total = 0.0
    iters_list = []
    converged_all = True
    for seed in seeds:
        model = build_magnet_board(seed)
        start = {r: (model.by_ref[r].x_mm, model.by_ref[r].y_mm) for r in REFS}
        start_intra = _pooled_intra(start)
        prop = run_placement(model, params)
        final = {}
        for r in REFS:
            fp = model.by_ref[r]
            dx, dy, _ = prop.deltas.get(fp.uuid, (0.0, 0.0, 0.0))
            final[r] = (fp.x_mm + dx, fp.y_mm + dy)
        final_intra = _pooled_intra(final)
        tighten_ratios.append(final_intra / start_intra if start_intra else 1.0)
        # Boundary: final centroid must sit inside region bbox (with courtyard margin).
        min_x, max_x, min_y, max_y = model.board_regions[0].bbox
        m = COURTYARD_HALF_W + 0.5
        for r in REFS:
            fx, fy = final[r]
            if not (min_x + m <= fx <= max_x - m and min_y + m <= fy <= max_y - m):
                boundary_violations += 1
        overlap_total += _final_overlap_area_mm2(model, prop)
        iters_list.append(prop.iterations)
        if prop.iterations >= params.max_iterations:
            converged_all = False
    return {
        "tighten_mean": sum(tighten_ratios) / len(tighten_ratios),
        "tighten_max": max(tighten_ratios),
        "boundary_violations": boundary_violations,
        "overlap_mm2": overlap_total / len(seeds),
        "iters_mean": sum(iters_list) / len(iters_list),
        "iters_max": max(iters_list),
        "converged_all": converged_all,
    }


def score(metrics):
    """Single scalar for ranking: tightening dominates, violations penalized."""
    s = metrics["tighten_mean"]
    s += 0.5 * metrics["boundary_violations"]
    s += 0.1 * metrics["overlap_mm2"]
    if not metrics["converged_all"]:
        s += 0.25
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description="SI magnet calibration sweep")
    ap.add_argument("--quick", action="store_true", help="24-combo smoke sweep")
    ap.add_argument("--max-iters", type=int, default=200)
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 7, 123])
    ap.add_argument("--out", type=str, default="")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv)

    if args.quick:
        grid = {
            "attraction_k_N": [0.02, 0.05, 0.1],
            "wall_k_N": [0.01, 0.03],
            "boundary_k_N": [0.005, 0.01],
            "vmax_m_s": [1.0],
        }
    else:
        grid = {
            "attraction_k_N": [0.02, 0.04, 0.06, 0.1],
            "wall_k_N": [0.01, 0.03, 0.08],
            "boundary_k_N": [0.005, 0.01],
            "vmax_m_s": [1.0, 1.5],
        }
    # Fixed for this round (repulsion negligible at clustering scales;
    # damping/dt explored after the force balance is sane). Mirrors the
    # calibrated engine defaults (docs/si_force_laws.md section 3).
    fixed = dict(
        max_iterations=args.max_iters,
        repulsion_k_N_m2=2e-9,
        wall_halo_m=0.006,
        wall_area_ref_m2=4e-6,
        ghost_k_N=5e-5,
        damping_Ns_m=0.12,
        friction_N=1e-6,
        dt_s=1e-3,
        dt_min_s=2e-4,
        dt_max_s=2e-3,
        convergence_eps_m=2e-5,
    )
    keys = list(grid)
    combos = list(itertools.product(*[grid[k] for k in keys]))
    print(f"magnet sweep: {len(combos)} combos x {len(args.seeds)} seeds")
    t0 = time.perf_counter()
    rows = []
    for combo in combos:
        pdict = dict(fixed)
        pdict.update(dict(zip(keys, combo)))
        params = PlacementParams(**pdict)
        try:
            m = evaluate(params, args.seeds)
        except Exception as e:  # never let one combo kill the sweep
            m = {
                "error": str(e),
                "tighten_mean": 9.0,
                "tighten_max": 9.0,
                "boundary_violations": 99,
                "overlap_mm2": 99.0,
                "iters_mean": args.max_iters,
                "iters_max": args.max_iters,
                "converged_all": False,
            }
        rows.append({"params": pdict, "metrics": m, "score": score(m)})
    rows.sort(key=lambda r: r["score"])
    dt = time.perf_counter() - t0
    print(f"done in {dt:.1f}s")
    print(f"{'score':>6} {'tight':>6} {'bv':>3} {'ovlp':>6} {'iters':>6}  params")
    for r in rows[: args.top]:
        m = r["metrics"]
        p = r["params"]
        print(
            f"{r['score']:6.3f} {m['tighten_mean']:6.3f} {m['boundary_violations']:3d} "
            f"{m['overlap_mm2']:6.2f} {m['iters_mean']:6.0f}  "
            f"attr={p['attraction_k_N']} wall={p['wall_k_N']} "
            f"bnd={p['boundary_k_N']} vmax={p['vmax_m_s']}"
        )
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"wrote {args.out}")
    best = rows[0]
    print("\nbest:")
    print(json.dumps({"params": best["params"], "metrics": best["metrics"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
