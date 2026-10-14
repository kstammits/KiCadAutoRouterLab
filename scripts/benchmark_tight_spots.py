#!/usr/bin/env python3
"""Tight-spot benchmark: cramped routing scenarios + cost-model sweep.

Covers the DCCF VIN situation (tiny SMD terminal inside a foreign
cluster, ~1000-cell tolled corridor) and any future tight spots: each
case routes with bounded budgets and reports success/expansions/time so
halo retunes, heuristic changes, or push/rip-up strategies can be
compared apples-to-apples.

The sweep varies what the user cannot: heuristic weight and soft-halo
scale (hard exclusion rings are preserved). Everything runs with small
expansion caps so the full matrix finishes in about a minute — the
point is fast signal on hard nets, not hero runs.

Usage (under the project .venv)::

    .venv/bin/python scripts/benchmark_tight_spots.py [--quick] [--out PATH]

Results print as a table and are saved as JSON (default
docs/benchmarks/tight_spots_<date>.json).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import board_model  # noqa: E402
from kicad_autorouter.routing import (  # noqa: E402
    build_occupancy_grid,
    create_grid_from_model,
)
from kicad_autorouter.routing.obstacles import (  # noqa: E402
    build_fanout_cost,
    build_via_block_mask,
)
from kicad_autorouter.routing.pipeline import (  # noqa: E402
    RoutingParams,
    _build_tht_via_mask,
    _terminals_for_net,
    resolve_clearance,
    resolve_track_width,
    via_halo_radius_cells,
)
from kicad_autorouter.routing.router import (  # noqa: E402
    SECONDARY_BLOCKED,
    CostMap,
    SingleNetRouter,
)
from kicad_autorouter.sexpr import parse_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

BUDGET = 100_000
RETRIES = 2  # initial window + 1 grown + full board


def scale_secondary(secondary: np.ndarray, factor: float) -> np.ndarray:
    """Scale soft halo tolls, preserving hard exclusion rings.

    Cells >= SECONDARY_BLOCKED (foreign-THT rings) are impassable by
    construction and stay untouched; everything else scales. This is
    what a halo retune would change, evaluated without editing costs.
    """
    out = secondary.copy()
    soft = (out > 0) & (out < SECONDARY_BLOCKED)
    out[soft] = (out[soft].astype(np.float32) * factor).astype(out.dtype)
    return out


def spot_facts(model, grid, net: str, term_idx: int) -> dict:
    """One-line tight-spot dossier for a terminal (for future boards)."""
    from kicad_autorouter.routing.pipeline import RoutingParams as RP
    params = RP(grid_resolution_mm=grid.resolution_mm)
    clearance_mm = resolve_clearance(model, params, net)
    secondary = build_fanout_cost(
        model, grid, net, clearance_mm=clearance_mm,
        track_half_mm=0.25,
    )
    vbm = build_via_block_mask(
        model, grid, clearance_mm=clearance_mm, track_half_mm=0.25
    )
    terms = _terminals_for_net(model, grid, net)
    c, r, lyr = terms[term_idx]
    return {
        "net": net,
        "terminals": len(terms),
        "terminal": [c, r, lyr],
        "own_cell_secondary": int(secondary[lyr, r, c]),
        "own_cell_via_blocked": bool(vbm[r, c]),
    }


def run_case(terminals, secondary, router, label: str, config: str) -> dict:
    """Route one terminal set; record success/expansions/time/cost."""
    t0 = time.perf_counter()
    result = router.route(terminals, secondary)
    elapsed = time.perf_counter() - t0
    return {
        "case": label,
        "config": config,
        "pairs": len(terminals),
        "success": result.success,
        "expansions": result.expansions,
        "time_s": round(elapsed, 3),
        "cost": round(result.cost, 1),
        "path_len": len(result.path),
        "exp_per_s": (
            round(result.expansions / elapsed, 1) if elapsed > 0 else None
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="control + tight pair only")
    ap.add_argument("--out", default=None, help="results JSON path")
    ap.add_argument("--budget", type=int, default=BUDGET,
                    help="max A* expansions per search")
    args = ap.parse_args()

    model = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    params = RoutingParams(grid_resolution_mm=0.1)
    grid = create_grid_from_model(model, 0.1, 2.0)
    cost_grid = build_occupancy_grid(
        model, grid, default_clearance_mm=params.clearance_mm
    )
    width_mm = resolve_track_width(model, params, "VIN", ("VIN",))
    clearance_mm = resolve_clearance(model, params, "VIN")
    tht = _build_tht_via_mask(model, grid)
    vbm = build_via_block_mask(
        model, grid, clearance_mm=clearance_mm,
        track_half_mm=width_mm / 2.0,
    )
    base_secondary = build_fanout_cost(
        model, grid, "VIN", clearance_mm=clearance_mm,
        track_half_mm=width_mm / 2.0,
    )
    via_radius = via_halo_radius_cells(
        params.via_size_mm, clearance_mm, width_mm / 2.0, 0.1
    )
    terms = _terminals_for_net(model, grid, "VIN")

    facts = spot_facts(model, grid, "VIN", 2)
    print("tight spot: VIN terminal", facts["terminal"],
          f"own_cell_secondary={facts['own_cell_secondary']}",
          f"via_blocked={facts['own_cell_via_blocked']}")

    cases = {
        "pair-control(C6-Q1)": [terms[0], terms[1]],
        "pair-tight(Q1-Q2)": [terms[1], terms[2]],
    }
    if not args.quick:
        cases["full-VIN"] = terms
    sweep = [(3.0, 1.0)]
    if not args.quick:
        sweep += [(1.0, 1.0), (3.0, 0.3), (1.0, 0.3)]

    # The control pair needs ~400k expansions (measured); tight cases
    # stay on the small fail-fast budget.
    budgets = {"pair-control(C6-Q1)": 500_000}
    rows = []
    for label, term_set in cases.items():
        for weight, halo in sweep:
            router = SingleNetRouter(
                grid, cost_grid, CostMap(heuristic_weight=weight),
                tht, vbm,
                max_expansions=budgets.get(label, args.budget),
                window_retries=RETRIES,
            )
            router.via_clearance_cells = via_radius
            secondary = (
                base_secondary if halo == 1.0
                else scale_secondary(base_secondary, halo)
            )
            rows.append(run_case(
                term_set, secondary, router, label,
                f"w={weight} halo={halo}",
            ))

    print(f"\n{'case':<22}{'config':<16}{'ok':>4}{'expans':>9}"
          f"{'time_s':>8}{'cost':>9}  exp/s")
    for r in rows:
        print(f"{r['case']:<22}{r['config']:<16}"
              f"{('Y' if r['success'] else '.'):>4}{r['expansions']:>9}"
              f"{r['time_s']:>8.3f}{r['cost']:>9.1f}  {r['exp_per_s']}")

    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks"
        / f"tight_spots_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"budget": args.budget, "retries": RETRIES,
         "spot_facts": facts, "cases": rows}, indent=2) + "\n")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
