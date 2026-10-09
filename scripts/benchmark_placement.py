#!/usr/bin/env python3
"""Placement benchmark: per-iteration cost vs part count + hotspot profile.

Measures ``run_placement`` on a fixture ladder (minimal -> tube111 -> DCCF)
plus synthetic boards with controlled footprint/pad counts, then fits::

    ms_per_iter ~= a * pads^2 + b * footprints^2 + c

The ``pads^2`` term captures the vectorized all-pairs repulsion; the
``footprints^2`` term captures the per-pair shapely courtyard loop (a
Python-level loop over footprint pairs every iteration — the suspected
hotspot on many-footprint boards; see placement.py ``_compute_total_force``).

Usage (under the project .venv)::

    .venv/bin/python scripts/benchmark_placement.py [--quick] [--out PATH]

--quick skips DCCF and to-convergence runs (fast CI smoke). Results print
as a table and are saved as JSON (default
docs/benchmarks/placement_<date>.json) for future planning — re-run as
placement features land so estimates stay honest.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import (  # noqa: E402
    BoardModel,
    Footprint,
    NetConnection,
    Pad,
    Point,
)
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402
from kicad_autorouter.board_model import board_model  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# Fixed iteration budget for per-iter timing: isolates per-step cost from
# convergence variance (a high convergence_eps forces the full budget).
TIMING_BUDGET = 50
TIMING_EPS = 1e-12


def make_synthetic(n_fp: int, pads_per_fp: int = 4, seed: int = 7) -> BoardModel:
    """Build a deterministic synthetic board: grid footprints, daisy nets.

    Footprints sit on a jittered grid with rectangular courtyards; pads of
    consecutive footprints share daisy-chained nets so attraction has edges.
    Edge.Cuts bounds the board (else the virtual region is unbounded and
    the boundary force is meaningless).
    """
    rng = np.random.default_rng(seed)
    cols = int(np.ceil(np.sqrt(n_fp)))
    fps: list[Footprint] = []
    nets: dict[str, tuple[NetConnection, ...]] = {}
    pitch, size = 12.0, 4.0
    for i in range(n_fp):
        gx, gy = i % cols, i // cols
        x = gx * pitch + float(rng.uniform(-1.0, 1.0))
        y = gy * pitch + float(rng.uniform(-1.0, 1.0))
        # Pad net names match one of the daisy chains below (informational;
        # attraction edges come from BoardModel.nets).
        pads = tuple(
            Pad(
                number=str(k + 1),
                net_name=f"SYN{min(i, n_fp - 2)}_{k}",
                position=Point(x + (k - pads_per_fp / 2) * 1.0, y),
                size_mm=(1.0, 1.0),
            )
            for k in range(pads_per_fp)
        )
        c = ((x - size, y - size), (x + size, y - size), (x + size, y + size), (x - size, y + size))
        courtyard = tuple(
            (Point(ax, ay), Point(bx, by)) for (ax, ay), (bx, by) in zip(c, c[1:] + c[:1])
        )
        fps.append(
            Footprint(
                ref=f"U{i}",
                footprint_id="synthetic:QFP",
                layer="F.Cu",
                x_mm=x,
                y_mm=y,
                angle_deg=0.0,
                pads=pads,
                courtyard=courtyard,
                uuid=f"synth-{i:04d}",
            )
        )
    # Daisy-chain: pad k of fp i shares a net with pad k of fp i+1.
    conns: dict[str, list[NetConnection]] = {}
    for i in range(n_fp - 1):
        for k in range(pads_per_fp):
            name = f"SYN{i}_{k}"
            conns.setdefault(name, []).extend(
                [NetConnection(ref=f"U{i}", pad_number=str(k + 1)),
                 NetConnection(ref=f"U{i + 1}", pad_number=str(k + 1))]
            )
    nets = {name: tuple(cs) for name, cs in conns.items()}
    extent = cols * pitch + 10.0
    edge_cuts = (
        (Point(0.0, 0.0), Point(extent, 0.0)),
        (Point(extent, 0.0), Point(extent, extent)),
        (Point(extent, extent), Point(0.0, extent)),
        (Point(0.0, extent), Point(0.0, 0.0)),
    )
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets=nets,
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=edge_cuts,
        # Single virtual region (region_idx 0): without this mapping every
        # footprint reads as immovable and the sim returns 0 iterations.
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def time_case(name: str, model: BoardModel, budget: int = TIMING_BUDGET) -> dict:
    """Time a fixed-budget run; report per-iteration cost + convergence run."""
    n_pads = sum(len(fp.pads) for fp in model.footprints)
    n_fp = len(model.footprints)
    params = PlacementParams(max_iterations=budget, convergence_eps_mm=TIMING_EPS)
    t0 = time.perf_counter()
    proposal = run_placement(model, params)
    fixed_s = time.perf_counter() - t0
    iters = max(proposal.iterations, 1)
    # To-convergence run with default params (convergence behavior, not just speed).
    conv_params = PlacementParams()
    t0 = time.perf_counter()
    conv = run_placement(model, conv_params)
    conv_s = time.perf_counter() - t0
    return {
        "board": name,
        "footprints": n_fp,
        "pads": n_pads,
        "fixed_budget_iters": proposal.iterations,
        "fixed_s": round(fixed_s, 3),
        "ms_per_iter": round(fixed_s / iters * 1000, 3),
        "converged_iters": conv.iterations,
        "converged_s": round(conv_s, 3),
        "moved": len(conv.deltas),
    }


def profile_case(model: BoardModel, budget: int = 20, top_n: int = 20) -> list[dict]:
    """cProfile one short run; return top cumulative entries ( Picklable)."""
    params = PlacementParams(max_iterations=budget, convergence_eps_mm=TIMING_EPS)
    pr = cProfile.Profile()
    pr.enable()
    run_placement(model, params)
    pr.disable()
    buf = io.StringIO()
    ps = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
    ps.print_stats(top_n)
    lines = []
    for func, (cc, nc, tt, ct, callers) in ps.stats.items():
        lines.append(
            {"func": f"{func[0].split('/')[-1]}:{func[1]}:{func[2]}",
             "calls": cc, "tottime": round(tt, 4), "cumtime": round(ct, 4)}
        )
    lines.sort(key=lambda d: -d["cumtime"])
    return lines[:top_n]


def fit_formula(rows: list[dict]) -> dict:
    """Least-squares fit: ms/iter = a*pads^2 + b*fps^2 + c."""
    A, y = [], []
    for r in rows:
        A.append([r["pads"] ** 2, r["footprints"] ** 2, 1.0])
        y.append(r["ms_per_iter"])
    coef, residuals, _, _ = np.linalg.lstsq(np.array(A), np.array(y), rcond=None)
    pred = np.array(A) @ coef
    err = np.abs(pred - np.array(y))
    return {
        "ms_per_iter = a*pads^2 + b*fps^2 + c": {
            "a": float(coef[0]), "b": float(coef[1]), "c": float(coef[2])
        },
        "max_abs_err_ms": float(err.max()) if len(err) else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="skip DCCF + convergence runs")
    ap.add_argument("--out", default=None, help="results JSON path")
    args = ap.parse_args()

    cases: list[tuple[str, BoardModel]] = [
        ("minimal", board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))),
        ("tube111", board_model(parse_file(FIXTURES / "tube111.kicad_pcb"))),
    ]
    for n in (8, 16, 32, 64):
        cases.append((f"synthetic_{n}fp", make_synthetic(n)))
    if not args.quick:
        cases.append(("DCCF", board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))))

    rows = [time_case(name, model) for name, model in cases]
    print(f"{'board':<14}{'fps':>6}{'pads':>7}{'ms/iter':>10}{'(50it)s':>9}{'conv_it':>9}{'conv_s':>9}")
    for r in rows:
        print(f"{r['board']:<14}{r['footprints']:>6}{r['pads']:>7}"
              f"{r['ms_per_iter']:>10.3f}{r['fixed_s']:>9.3f}"
              f"{r['converged_iters']:>9}{r['converged_s']:>9.3f}")

    formula = fit_formula(rows)
    print("\nFitted cost model:", json.dumps(formula, indent=2))

    # Profile a mid-size case (tube111 if present, else largest synthetic).
    prof_model = next(m for n, m in cases if n == "tube111")
    prof = profile_case(prof_model)
    print("\ncProfile top (tube111, 20 iters, cumulative):")
    for e in prof:
        print(f"  {e['cumtime']:>9.4f}s cum  {e['tottime']:>9.4f}s tot  {e['calls']:>7} calls  {e['func']}")

    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks" / f"placement_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"cases": rows, "formula": formula, "profile_top": prof}, indent=2) + "\n")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
