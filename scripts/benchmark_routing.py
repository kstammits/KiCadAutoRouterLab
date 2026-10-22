#!/usr/bin/env python3
"""Routing benchmark: end-to-end cost vs net count + stage profile.

Measures the current mode of operation: per-net A* over a windowed search
with a precomputed via-landing mask (``build_via_ok_mask`` via scipy
dilation), fanout/secondary halos, string-pull smoothing, and corridor
reservation — driven through the production ``route_nets`` entry point.

Stitching (GND continuity repair) and DRC are disabled by default so the
numbers reflect routing proper; pass ``--with-stitch`` to measure one
end-to-end case with stitching enabled.

Usage (under the project .venv)::

    .venv/bin/python scripts/benchmark_routing.py [--quick] [--out PATH] [--with-stitch]

--quick runs a CI smoke (DCCF 2 nets + tube111 1 net). Results print as
tables and are saved as JSON (default
docs/benchmarks/routing_<date>.json) for future planning — re-run as
routing features land so estimates stay honest.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import is_ground_net  # noqa: E402
from kicad_autorouter.routing import (  # noqa: E402
    build_occupancy_grid,
    create_grid_from_model,
)
from kicad_autorouter.routing.obstacles import (  # noqa: E402
    build_fanout_cost,
    build_via_block_mask,
    via_halo_radius_cells,
)
from kicad_autorouter.routing.pipeline import (  # noqa: E402
    RoutingParams,
    _build_tht_via_mask,
    resolve_clearance,
    resolve_track_width,
    route_nets,
)
from kicad_autorouter.routing.router import SECONDARY_BLOCKED  # noqa: E402
from kicad_autorouter.routing.via_sites import build_via_ok_mask  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402
from kicad_autorouter.board_model import board_model  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

POWER_NAMES = {"VIN", "+12V", "-12V", "VDD", "VCC", "VEE", "+5V", "-5V", "+3V3"}


def load_board(fixture_name: str):
    """Load a board fixture (tube111 or DCCF.sved)."""
    path = FIXTURES / f"{fixture_name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{fixture_name}.sved.kicad_pcb"
    return board_model(parse_file(path))


def pick_nets(model, k: int) -> list[str]:
    """Deterministic signal-net pick: 2-4 pad nets, smallest first.

    Power/ground nets are skipped (they route under different widths and
    the GND stitcher owns ground continuity).
    """
    cands = []
    for net_name, conns in model.nets.items():
        if not 2 <= len(conns) <= 4:
            continue
        if net_name.upper() in POWER_NAMES or is_ground_net(net_name):
            continue
        cands.append((len(conns), net_name))
    cands.sort()
    return [name for _, name in cands[:k]]


def pick_power_nets(model, k: int) -> list[str]:
    """Deterministic power-net pick: 2-8 pad nets, smallest first."""
    cands = []
    for net_name, conns in model.nets.items():
        if not 2 <= len(conns) <= 8:
            continue
        if net_name.upper() in POWER_NAMES or is_ground_net(net_name):
            cands.append((len(conns), net_name))
    cands.sort()
    return [name for _, name in cands[:k]]


def pick_all_signal_nets(model) -> list[str]:
    """All signal nets (2-4 pads) sorted by pad count."""
    cands = []
    for net_name, conns in model.nets.items():
        if not 2 <= len(conns) <= 4:
            continue
        if net_name.upper() in POWER_NAMES or is_ground_net(net_name):
            continue
        cands.append((len(conns), net_name))
    cands.sort()
    return [name for _, name in cands]


def pick_all_power_nets(model) -> list[str]:
    """All power nets (2-8 pads) sorted by pad count."""
    cands = []
    for net_name, conns in model.nets.items():
        if not 2 <= len(conns) <= 8:
            continue
        if net_name.upper() in POWER_NAMES or is_ground_net(net_name):
            cands.append((len(conns), net_name))
    cands.sort()
    return [name for _, name in cands]


def micro_case(board_name: str, model, grid, params: RoutingParams) -> dict:
    """Time the one-off per-board / per-net stage costs (the fixed stages).

    ``via_ok_mask_s`` is one scipy dilation rather than per-obstacle Python
    stamping.
    """
    cost_grid = build_occupancy_grid(
        model, grid, default_clearance_mm=params.clearance_mm
    )

    t0 = time.perf_counter()
    tht_mask = _build_tht_via_mask(model, grid)
    tht_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    via_block = build_via_block_mask(
        model, grid,
        clearance_mm=params.clearance_mm,
        track_half_mm=params.track_width_mm / 2.0,
    )
    via_block_s = time.perf_counter() - t0

    # Representative net for the per-net stages (first pick).
    signal_nets = pick_nets(model, 1)
    if not signal_nets:
        # Fallback: use first power net or skip
        power_nets = pick_power_nets(model, 1)
        if not power_nets:
            return {
                "board": board_name,
                "grid_cells_M": round(
                    len(grid.layers) * grid.width_cells * grid.height_cells / 1e6, 2
                ),
                "net": "none",
                "via_radius_cells": 0,
                "occupancy_s": round(
                    _time_fn(build_occupancy_grid, model, grid,
                             default_clearance_mm=params.clearance_mm),
                    3,
                ),
                "tht_mask_s": round(tht_s, 3),
                "via_block_mask_s": round(via_block_s, 3),
                "fanout_s": 0.0,
                "via_ok_mask_s": 0.0,
                "via_ok_frac": 0.0,
            }
        net_name = power_nets[0]
    else:
        net_name = signal_nets[0]
    width_mm = resolve_track_width(model, params, net_name, ())
    clearance_mm = resolve_clearance(model, params, net_name)

    t0 = time.perf_counter()
    secondary = build_fanout_cost(
        model, grid, net_name,
        clearance_mm=clearance_mm,
        track_half_mm=width_mm / 2.0,
    )
    fanout_s = time.perf_counter() - t0

    radius = via_halo_radius_cells(
        params.via_size_mm, clearance_mm, width_mm / 2.0,
        params.grid_resolution_mm,
    )
    t0 = time.perf_counter()
    mask = build_via_ok_mask(
        cost_grid, via_block, tht_mask, secondary,
        radius, 100, SECONDARY_BLOCKED,
    )
    via_ok_s = time.perf_counter() - t0

    return {
        "board": board_name,
        "grid_cells_M": round(
            len(grid.layers) * grid.width_cells * grid.height_cells / 1e6, 2
        ),
        "net": net_name,
        "via_radius_cells": int(radius),
        "occupancy_s": round(
            _time_fn(build_occupancy_grid, model, grid,
                     default_clearance_mm=params.clearance_mm),
            3,
        ),
        "tht_mask_s": round(tht_s, 3),
        "via_block_mask_s": round(via_block_s, 3),
        "fanout_s": round(fanout_s, 3),
        "via_ok_mask_s": round(via_ok_s, 3),
        "via_ok_frac": round(float(mask.mean()), 3),
    }


def _time_fn(fn, *args, **kwargs) -> float:
    t0 = time.perf_counter()
    fn(*args, **kwargs)
    return time.perf_counter() - t0


def time_case(
    board_name: str,
    net_names: list[str],
    params: RoutingParams,
    label: str | None = None,
) -> dict:
    """Route the first ``len(net_names)`` nets end-to-end via ``route_nets``."""
    model = load_board(board_name)
    result, _, grid = route_nets(
        model, net_names, set(),
        params=params,
        progress_cb=None,
        should_cancel=None,
    )
    t = result.timings
    n = len(net_names)
    total = t.get("total_s", 0.0)
    return {
        "board": label or board_name,
        "nets_requested": n,
        "net_names": net_names,
        "nets_routed": result.nets_routed,
        "nets_failed": result.nets_failed,
        "expansions": result.expansions,
        "total_s": round(total, 3),
        "route_s": round(t.get("route_s", 0.0), 3),
        "astar_s": round(t.get("astar_s", 0.0), 3),
        "smooth_s": round(t.get("smooth_s", 0.0), 3),
        "occupancy_s": round(t.get("occupancy_s", 0.0), 3),
        "s_per_net": round(total / max(n, 1), 3),
        "ms_per_kexp": (
            round(total / max(result.expansions, 1) * 1e6, 1)
            if result.expansions else None
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="CI smoke: few nets")
    ap.add_argument("--out", default=None, help="results JSON path")
    ap.add_argument("--resolution", type=float, default=0.1,
                    help="grid resolution in mm")
    ap.add_argument("--max-expansions", type=int, default=500_000,
                    help="A* expansion cap per search (bounds failed-net cost)")
    ap.add_argument("--with-stitch", action="store_true",
                    help="also measure one case with GND stitching enabled")
    args = ap.parse_args()

    params = RoutingParams(
        grid_resolution_mm=args.resolution,
        max_expansions=args.max_expansions,
        max_attempts_per_net=1,
        stitch_gnd_vias=False,
        run_drc=False,
    )

    if args.quick:
        plan = [("DCCF", [2]), ("tube111", [1])]
        power_plan = [("DCCF", [1]), ("tube111", [1])]
    else:
        plan = [("DCCF", [1, 2, 5, 10, 20]), ("tube111", [1, 2, 5])]
        power_plan = [("DCCF", [1, 2, 4]), ("tube111", [1, 2])]

    # Micro (fixed-stage) profile, once per board.
    micros = []
    for board_name in dict.fromkeys(b for b, _ in plan + [("minimal", [1])]):
        model = load_board(board_name)
        grid = create_grid_from_model(
            model, params.grid_resolution_mm, params.margin_mm
        )
        micros.append(micro_case(board_name, model, grid, params))

    print(f"{'board':<10}{'cells_M':>9}{'net':>22}"
          f"{'occup':>8}{'tht':>8}{'vblock':>8}{'fanout':>8}{'via_ok':>8}")
    for m in micros:
        print(f"{m['board']:<10}{m['grid_cells_M']:>9.2f}{m['net']:>22}"
              f"{m['occupancy_s']:>8.3f}{m['tht_mask_s']:>8.3f}"
              f"{m['via_block_mask_s']:>8.3f}{m['fanout_s']:>8.3f}"
              f"{m['via_ok_mask_s']:>8.3f}")

    # Scaling runs: route first-k signal nets fresh each time.
    rows = []
    for board_name, counts in plan:
        model = load_board(board_name)
        for k in counts:
            nets = pick_nets(model, k)
            if nets:
                rows.append(time_case(board_name, nets, params))

    # Power net scaling runs (wide tracks, different clearances).
    for board_name, counts in power_plan:
        model = load_board(board_name)
        for k in counts:
            nets = pick_power_nets(model, k)
            if nets:
                rows.append(time_case(board_name, nets, params, label=f"{board_name}_power"))

    print(f"\n{'board':<16}{'nets':>5}{'routed':>7}{'failed':>7}"
          f"{'expans':>9}{'total_s':>9}{'astar_s':>9}{'smooth_s':>9}"
          f"{'s/net':>8}")
    for r in rows:
        print(f"{r['board']:<16}{r['nets_requested']:>5}"
              f"{r['nets_routed']:>7}{r['nets_failed']:>7}"
              f"{r['expansions']:>9}{r['total_s']:>9.3f}"
              f"{r['astar_s']:>9.3f}{r['smooth_s']:>9.3f}"
              f"{r['s_per_net']:>8.3f}")

    stitch_row = None
    if args.with_stitch:
        model = load_board("DCCF")
        nets = pick_nets(model, 5)
        stitch_params = RoutingParams(
            grid_resolution_mm=args.resolution,
            max_expansions=args.max_expansions,
            max_attempts_per_net=1,
            stitch_gnd_vias=True,
            run_drc=False,
        )
        stitch_row = time_case("DCCF", nets, stitch_params,
                                label="DCCF+stitch")
        print(f"\nwith stitch: {stitch_row['total_s']:.3f}s total "
              f"({stitch_row['route_s']:.3f}s route) for "
              f"{stitch_row['nets_requested']} nets")

    import json
    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks" / f"routing_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"params": params.to_dict(), "micro": micros, "cases": rows,
         "stitch_case": stitch_row}, indent=2) + "\n")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
