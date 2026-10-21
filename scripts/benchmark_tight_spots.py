#!/usr/bin/env python3
"""Tight-spot benchmark: cramped routing scenarios + cost-model sweep.

Covers the DCCF VIN situation (tiny SMD terminal inside a foreign
cluster, ~1000-cell tolled corridor) and tube111 tight spots (power nets
+12V/-12V and signal nets with high secondary cost). Each case routes
with bounded budgets and reports success/expansions/time so halo retunes,
heuristic changes, or push/rip-up strategies can be compared apples-to-apples.

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
    _rip_up_nets_from_model,
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


def build_board_config(model, net_name: str, params: RoutingParams, grid):
    """Build routing config for a specific net on a board."""
    cost_grid = build_occupancy_grid(
        model, grid, default_clearance_mm=params.clearance_mm
    )
    width_mm = resolve_track_width(model, params, net_name, (net_name,))
    clearance_mm = resolve_clearance(model, params, net_name)
    tht = _build_tht_via_mask(model, grid)
    vbm = build_via_block_mask(
        model, grid, clearance_mm=clearance_mm,
        track_half_mm=width_mm / 2.0,
    )
    base_secondary = build_fanout_cost(
        model, grid, net_name, clearance_mm=clearance_mm,
        track_half_mm=width_mm / 2.0,
    )
    via_radius = via_halo_radius_cells(
        params.via_size_mm, clearance_mm, width_mm / 2.0, 0.1
    )
    terms = _terminals_for_net(model, grid, net_name)
    return {
        "cost_grid": cost_grid,
        "tht": tht,
        "vbm": vbm,
        "base_secondary": base_secondary,
        "via_radius": via_radius,
        "terms": terms,
        "width_mm": width_mm,
        "clearance_mm": clearance_mm,
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


def get_tube111_tight_spots(model, grid, params: RoutingParams) -> list[dict]:
    """Identify tight spots on tube111: nets with terminals having secondary=500."""
    tight_spots = []

    # Power nets with many secondary=500 terminals
    for net_name in ["+12V", "-12V"]:
        width_mm = resolve_track_width(model, params, net_name, (net_name,))
        clearance_mm = resolve_clearance(model, params, net_name)
        secondary = build_fanout_cost(
            model, grid, net_name, clearance_mm=clearance_mm,
            track_half_mm=width_mm / 2.0,
        )
        terms = _terminals_for_net(model, grid, net_name)
        # Find terminals with secondary=500
        tight_terms = []
        for i, (c, r, lyr) in enumerate(terms):
            if int(secondary[lyr, r, c]) >= 500:
                tight_terms.append(i)
        if tight_terms:
            tight_spots.append({
                "net": net_name,
                "tight_terminals": tight_terms,
                "all_terms": terms,
            })

    # Signal nets with secondary=500 terminals
    for net_name in ["C2Output", "C2OutCV", "C2OutCVLED", "grid_cv", "Net-(R31-Pad1)"]:
        width_mm = resolve_track_width(model, params, net_name, ())
        clearance_mm = resolve_clearance(model, params, net_name)
        secondary = build_fanout_cost(
            model, grid, net_name, clearance_mm=clearance_mm,
            track_half_mm=width_mm / 2.0,
        )
        terms = _terminals_for_net(model, grid, net_name)
        tight_terms = []
        for i, (c, r, lyr) in enumerate(terms):
            if int(secondary[lyr, r, c]) >= 500:
                tight_terms.append(i)
        if tight_terms:
            tight_spots.append({
                "net": net_name,
                "tight_terminals": tight_terms,
                "all_terms": terms,
            })

    return tight_spots


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="control + tight pair only")
    ap.add_argument("--out", default=None, help="results JSON path")
    ap.add_argument("--budget", type=int, default=BUDGET,
                    help="max A* expansions per search")
    args = ap.parse_args()

    # Boards to test
    boards = ["DCCF.sved", "tube111"]

    all_rows = []
    all_facts = {}

    for board_name in boards:
        model = board_model(parse_file(FIXTURES / f"{board_name}.kicad_pcb"))
        params = RoutingParams(grid_resolution_mm=0.1)
        grid = create_grid_from_model(model, 0.1, 2.0)

        if board_name == "DCCF.sved":
            # DCCF VIN tight spot
            net_name = "VIN"
            width_mm = resolve_track_width(model, params, net_name, ("VIN",))
            clearance_mm = resolve_clearance(model, params, net_name)
            tht = _build_tht_via_mask(model, grid)
            vbm = build_via_block_mask(
                model, grid, clearance_mm=clearance_mm,
                track_half_mm=width_mm / 2.0,
            )
            base_secondary = build_fanout_cost(
                model, grid, net_name, clearance_mm=clearance_mm,
                track_half_mm=width_mm / 2.0,
            )
            via_radius = via_halo_radius_cells(
                params.via_size_mm, clearance_mm, width_mm / 2.0, 0.1
            )
            terms = _terminals_for_net(model, grid, net_name)

            facts = spot_facts(model, grid, net_name, 2)
            all_facts["DCCF_VIN"] = facts
            print(f"tight spot: {board_name} {net_name} terminal", facts["terminal"],
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

            budgets = {"pair-control(C6-Q1)": 500_000}
            cost_grid = build_occupancy_grid(
                model, grid, default_clearance_mm=params.clearance_mm
            )

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
                    all_rows.append(run_case(
                        term_set, secondary, router, f"DCCF_{label}",
                        f"w={weight} halo={halo}",
                    ))

        else:
            # tube111 tight spots
            tight_spots = get_tube111_tight_spots(model, grid, params)
            print(f"tube111 tight spots found: {len(tight_spots)} nets")
            for ts in tight_spots:
                print(f"  {ts['net']}: tight terminals={ts['tight_terminals']}")

            sweep = [(3.0, 1.0)]
            if not args.quick:
                sweep += [(1.0, 1.0), (3.0, 0.3), (1.0, 0.3)]

            budgets = {}

            for ts in tight_spots:
                net_name = ts["net"]
                terms = ts["all_terms"]
                tight_terms = ts["tight_terminals"]

                # Create cases for this net
                cases = {}
                if len(terms) >= 2:
                    cases[f"{net_name}_control"] = [terms[0], terms[1]]
                if len(tight_terms) >= 1 and len(terms) >= 2:
                    tight_idx = tight_terms[0]
                    other_idx = 0 if tight_idx != 0 else 1
                    cases[f"{net_name}_tight"] = [terms[tight_idx], terms[other_idx]]
                if not args.quick and len(terms) >= 3:
                    cases[f"{net_name}_full"] = terms

                # Record facts for the first tight terminal
                if tight_terms:
                    facts = spot_facts(model, grid, net_name, tight_terms[0])
                    all_facts[f"tube111_{net_name}"] = facts
                    print(f"  tight spot: tube111 {net_name} terminal", facts["terminal"],
                          f"own_cell_secondary={facts['own_cell_secondary']}",
                          f"via_blocked={facts['own_cell_via_blocked']}")

                # Build config for this net
                width_mm = resolve_track_width(model, params, net_name, (net_name,) if net_name in ["+12V", "-12V"] else ())
                clearance_mm = resolve_clearance(model, params, net_name)
                tht = _build_tht_via_mask(model, grid)
                vbm = build_via_block_mask(
                    model, grid, clearance_mm=clearance_mm,
                    track_half_mm=width_mm / 2.0,
                )
                base_secondary = build_fanout_cost(
                    model, grid, net_name, clearance_mm=clearance_mm,
                    track_half_mm=width_mm / 2.0,
                )
                via_radius = via_halo_radius_cells(
                    params.via_size_mm, clearance_mm, width_mm / 2.0, 0.1
                )

                # Rip up this net's tracks so we're routing as if unconnected
                model_ripped = _rip_up_nets_from_model(model, [net_name])
                cost_grid = build_occupancy_grid(
                    model_ripped, grid, default_clearance_mm=params.clearance_mm
                )

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
                        all_rows.append(run_case(
                            term_set, secondary, router, f"tube111_{label}",
                            f"w={weight} halo={halo}",
                        ))

    # Print comparison table.
    print(f"\n{'case':<30}{'config':<16}{'ok':>4}{'expans':>9}"
          f"{'time_s':>8}{'cost':>9}  exp/s")
    for r in all_rows:
        print(f"{r['case']:<30}{r['config']:<16}"
              f"{('Y' if r['success'] else '.'):>4}{r['expansions']:>9}"
              f"{r['time_s']:>8.3f}{r['cost']:>9.1f}  {r['exp_per_s']}")

    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks"
        / f"tight_spots_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"budget": args.budget, "retries": RETRIES,
         "spot_facts": all_facts, "cases": all_rows}, indent=2) + "\n")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()