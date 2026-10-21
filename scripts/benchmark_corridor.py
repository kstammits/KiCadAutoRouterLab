#!/usr/bin/env python3
"""Corridor guidance benchmark: does the coarse corridor help, and when?

Routes nets on tube111 and DCCF with corridor guidance on vs off,
reporting board fill/density metrics plus per-run corridor stats
(trigger rate, success rate, corridor cell fraction, expansions, time).

This is a standalone benchmark script (not part of the pytest suite).

Usage (under the project .venv)::

    .venv/bin/python scripts/benchmark_corridor.py [--quick] [--out PATH]

--quick runs a CI smoke (DCCF 6 nets + tube111 4 nets).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import is_ground_net
from kicad_autorouter.routing import (
    build_occupancy_grid,
    create_grid_from_model,
)
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    route_nets,
)
from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.board_model import board_model

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

POWER_NAMES = {"VIN", "+12V", "-12V", "VDD", "VCC", "VEE", "+5V", "-5V", "+3V3"}


def load_board(fixture_name: str):
    path = FIXTURES / f"{fixture_name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{fixture_name}.sved.kicad_pcb"
    return board_model(parse_file(path))


def pick_nets(model, k_power: int, k_signal: int) -> list[str]:
    """Pick power nets first (they're the tight spots), then signal nets.

    Power nets route with wide tracks and foreign halos — the cases where
    the initial window most often fails and corridor guidance matters.
    """
    power = []
    signal = []
    for net_name, conns in model.nets.items():
        if len(conns) < 2:
            continue
        upper = net_name.upper()
        if upper in POWER_NAMES or is_ground_net(net_name):
            power.append(net_name)
        else:
            signal.append(net_name)
    power.sort()
    signal.sort(key=lambda n: len(model.nets[n]))
    return power[:k_power] + signal[:k_signal]


def route_case(model, net_names, params: RoutingParams, label: str) -> dict:
    """Route nets cumulatively via route_nets; return timing + corridor stats."""
    t0 = time.perf_counter()
    result, cost_grid, grid = route_nets(
        model, net_names, set(),
        params=params,
        progress_cb=None,
        should_cancel=None,
    )
    wall = time.perf_counter() - t0
    t = result.timings
    return {
        "label": label,
        "nets_requested": len(net_names),
        "nets_routed": result.nets_routed,
        "nets_failed": result.nets_failed,
        "expansions": result.expansions,
        "wall_s": round(wall, 4),
        "total_s": round(t.get("total_s", 0.0), 4),
        "astar_s": round(t.get("astar_s", 0.0), 4),
        "occupancy_s": round(t.get("occupancy_s", 0.0), 4),
        "blocked_frac": round(t.get("blocked_frac", 0.0), 4),
        "occupied_frac": round(t.get("occupied_frac", 0.0), 4),
        "tolled_frac": round(t.get("tolled_frac", 0.0), 4),
        "corridor_tried": int(t.get("corridor_tried", 0)),
        "corridor_found": int(t.get("corridor_found", 0)),
        "corridor_used": int(t.get("corridor_used", 0)),
        "corridor_cells": int(t.get("corridor_cells", 0)),
        "corridor_use_rate": t.get("corridor_use_rate", 0.0),
        "grid_cells_M": round(t.get("grid_cells", 0) / 1e6, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="CI smoke: few nets")
    ap.add_argument("--out", default=None, help="results JSON path")
    ap.add_argument("--resolution", type=float, default=0.1,
                    help="grid resolution in mm")
    ap.add_argument("--max-expansions", type=int, default=500_000,
                    help="A* expansion cap per search")
    ap.add_argument("--window-margin", type=int, default=32,
                    help="initial A* window margin (cells); smaller => more corridor triggers")
    ap.add_argument("--window-margins", type=int, nargs="+", default=None,
                    help="sweep window margins (default [16, 32, 64])")
    args = ap.parse_args()

    window_margins = args.window_margins or [16, 32, 64]

    if args.quick:
        plan = [("DCCF", 2, 4), ("tube111", 1, 3), ("minimal", 0, 1)]
        window_margins = [32]  # single margin for quick
    else:
        plan = [("DCCF", 3, 6), ("tube111", 2, 5), ("minimal", 0, 1)]

    rows = []
    for board_name, k_power, k_signal in plan:
        model = load_board(board_name)
        grid = create_grid_from_model(
            model, args.resolution, 2.0
        )
        # Fill metrics from a fresh occupancy grid (board-level density).
        cost_grid = build_occupancy_grid(
            model, grid, default_clearance_mm=0.2
        )
        blocked = cost_grid >= 100
        fill = {
            "blocked_frac": round(float(blocked.mean()), 4),
            "occupied_frac": round(float((cost_grid > 0).mean()), 4),
            "tolled_frac": round(
                float(((cost_grid > 0) & (cost_grid < 100)).mean()), 4
            ),
        }

        nets = pick_nets(model, k_power, k_signal)
        if not nets:
            continue

        for margin in window_margins:
            for corridor in (True, False):
                params = RoutingParams(
                    grid_resolution_mm=args.resolution,
                    max_expansions=args.max_expansions,
                    max_attempts_per_net=1,
                    stitch_gnd_vias=False,
                    run_drc=False,
                    window_margin_cells=margin,
                    corridor=corridor,
                )
                tag = "on" if corridor else "off"
                row = route_case(model, nets, params,
                                 f"{board_name} P{k_power}S{k_signal} W{margin} corridor={tag}")
                row.update(fill)
                rows.append(row)

    # Print comparison table.
    print(f"{'board':<8}{'P':>3}{'S':>3}{'W':>4}{'corr':>6}{'routed':>7}{'failed':>7}"
          f"{'expans':>10}{'astar_s':>8}{'wall_s':>8}{'blk%':>6}{'occ%':>6}"
          f"{'tried':>7}{'found':>7}{'used':>7}{'cells':>9}{'use%':>6}")
    for r in rows:
        parts = r["label"].split()
        board = parts[0]
        rest = parts[1]
        kp = rest.split("P")[1].split("S")[0]
        ks = rest.split("S")[1].split("W")[0]
        wm = rest.split("W")[1].split(" ")[0] if "W" in rest else "0"
        print(f"{board:<8}{kp:>3}{ks:>3}{wm:>4}{'on' if 'on' in r['label'] else 'off':>6}"
              f"{r['nets_routed']:>7}{r['nets_failed']:>7}"
              f"{r['expansions']:>10}{r['astar_s']:>8.3f}{r['wall_s']:>8.3f}"
              f"{r['blocked_frac']*100:>6.1f}{r['occupied_frac']*100:>6.1f}"
              f"{r['corridor_tried']:>7}{r['corridor_found']:>7}"
              f"{r['corridor_used']:>7}{r['corridor_cells']:>9}"
              f"{r['corridor_use_rate']*100:>6.1f}")

    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks" / f"corridor_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"params": {"resolution": args.resolution,
                                          "max_expansions": args.max_expansions,
                                          "window_margins": window_margins},
                               "cases": rows}, indent=2) + "\n")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
