#!/usr/bin/env python3
"""A/B bench: Python vs numba A* neighbor expansion (quick scope).

Routes the same nets under ``router.ASTAR_USE_NUMBA`` False/True and
compares expansions (must be identical), astar_s, and ms/kexp. The njit
kernel is warmed once before timing so compile cost never lands in a row.

Usage:
    .venv/bin/python scripts/benchmark_lib_compare.py [--impl ours|numba|both] [--out PATH]

Quick plan mirrors benchmark_routing.py --quick: DCCF x2 + tube111 x1
signal nets, DCCF x1 + tube111 x1 power nets (resolution 0.1, 500k cap,
attempts=1, stitch/drc off). Full plans stay in benchmark_routing.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kicad_autorouter.routing import router as router_mod  # noqa: E402
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets  # noqa: E402

from benchmark_routing import load_board, pick_nets, pick_power_nets  # noqa: E402

try:
    import numba

    NUMBA_VERSION = numba.__version__
except ImportError:
    NUMBA_VERSION = None


def warm_kernel() -> None:
    """Compile the njit kernel off the clock with one throwaway route."""
    router_mod.ASTAR_USE_NUMBA = True
    model = load_board("tube111")
    nets = pick_nets(model, 1)
    route_nets(model, nets, set())


def time_case(board_name: str, net_names: list[str], params: RoutingParams) -> dict:
    model = load_board(board_name)
    t0 = time.perf_counter()
    result, _, _ = route_nets(model, net_names, set(), params=params)
    wall = time.perf_counter() - t0
    t = result.timings
    return {
        "board": board_name,
        "net_names": net_names,
        "nets_routed": result.nets_routed,
        "nets_failed": result.nets_failed,
        "expansions": result.expansions,
        "astar_s": round(t.get("astar_s", 0.0), 3),
        "smooth_s": round(t.get("smooth_s", 0.0), 3),
        "route_s": round(t.get("route_s", 0.0), 3),
        "wall_s": round(wall, 3),
        "ms_per_kexp": (
            round(t.get("astar_s", 0.0) / max(result.expansions, 1) * 1e6, 1)
            if result.expansions
            else None
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--impl", choices=("ours", "numba", "both"), default="both")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    params = RoutingParams(
        grid_resolution_mm=0.1,
        max_expansions=500_000,
        max_attempts_per_net=1,
        stitch_gnd_vias=False,
        run_drc=False,
    )
    impls = ("ours", "numba") if args.impl == "both" else (args.impl,)

    if "numba" in impls:
        t0 = time.perf_counter()
        warm_kernel()
        print(f"kernel warmup: {time.perf_counter() - t0:.2f}s (excluded from rows)")

    cases = []
    dccf = load_board("DCCF")
    tube = load_board("tube111")
    plan = [
        ("DCCF", pick_nets(dccf, 2)),
        ("tube111", pick_nets(tube, 1)),
        ("DCCF_power", pick_power_nets(dccf, 1)),
        ("tube111_power", pick_power_nets(tube, 1)),
    ]
    rows = []
    for impl in impls:
        router_mod.ASTAR_USE_NUMBA = impl == "numba"
        for board, nets in plan:
            row = time_case(board.split("_")[0], nets, params)
            row["impl"] = impl
            row["label"] = board
            rows.append(row)
            print(
                f"{impl:6s} {board:14s} nets={row['nets_routed']}/{len(nets)} "
                f"exp={row['expansions']:>7d} astar={row['astar_s']:.3f}s "
                f"ms/kexp={row['ms_per_kexp']}"
            )
    # Equivalence gate: same nets, same expansions, same outcomes.
    if args.impl == "both":
        for b in {r["label"] for r in rows}:
            o = next(r for r in rows if r["label"] == b and r["impl"] == "ours")
            n = next(r for r in rows if r["label"] == b and r["impl"] == "numba")
            assert (o["nets_routed"], o["nets_failed"], o["expansions"]) == (
                n["nets_routed"],
                n["nets_failed"],
                n["expansions"],
            ), f"divergence on {b}: {o} vs {n}"
        print("equivalence gate: identical nets_routed/failed/expansions on all cases")

    out = {
        "date": str(date.today()),
        "params": params.to_dict(),
        "numba_version": NUMBA_VERSION,
        "topology": getattr(router_mod, "ROUTER_TOPOLOGY", 4),
        "impls": list(impls),
        "cases": rows,
    }
    path = Path(args.out) if args.out else None
    if path is None:
        repo = Path(__file__).resolve().parent.parent
        path = repo / "docs" / "benchmarks" / f"libcompare_{date.today()}.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
