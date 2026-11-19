"""Routing re-route benchmark: full route vs move-footprint + re-route.

Measures where wall-time goes when a footprint moves and affected nets
must be re-routed (the practical re-route loop).

Usage:
    .venv/bin/python scripts/benchmark_routing.py \
        --board both --mode both --repeat 2 --out docs/benchmarks/routing_YYYY-MM-DD.json

Bench preset (bounded, deterministic): route_nets single pass
(trial-0 cost, the unit re-route pays), run_drc=False,
refill_zones=False, retry_stages N/A (route_with_retry not used),
max_attempts_per_net=1. Stitch stays ON (finish_routes cost is real).
Numba warms up on `minimal` before timing so JIT compile is excluded.
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
from statistics import median

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from kicad_autorouter.board_model import board_model  # noqa: E402
from kicad_autorouter.board_model.query import apply_deltas  # noqa: E402
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"
BOARDS = {
    "tube111": FIXTURES / "tube111.kicad_pcb",
    "dccf": FIXTURES / "DCCF.sved.kicad_pcb",
}

TIMING_KEYS = (
    "grid_build_s", "occupancy_s", "pour_s", "astar_s",
    "smooth_s", "smooth_pull_s", "smooth_fit_s", "smooth_verify_s",
    "smooth_verify_build_s", "route_s", "total_s",
)


def bench_params() -> RoutingParams:
    return RoutingParams(
        run_drc=False,
        refill_zones=False,
        stitch_gnd_vias=True,
        max_attempts_per_net=1,
        max_expansions_per_net=2_000_000,
    )


def routable_nets(model) -> list[str]:
    return [n for n, conns in model.nets.items() if len(conns) >= 2]


def pick_moved_footprint(model):
    """Footprint touching the most distinct nets (unlocked, has uuid)."""
    best, best_nets = None, set()
    for fp in model.footprints:
        if fp.locked or not fp.uuid:
            continue
        nets = {
            net for net, conns in model.nets.items()
            for c in conns if c.ref == fp.ref
        }
        if len(nets) > len(best_nets):
            best, best_nets = fp, nets
    return best, sorted(best_nets)


def summarize(result, wall_s: float) -> dict:
    timings = {
        k: v for k, v in result.timings.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    top = sorted(
        result.net_stats.items(),
        key=lambda kv: kv[1].get("wall_s", 0.0),
        reverse=True,
    )[:10]
    return {
        "wall_s": round(wall_s, 3),
        "nets_routed": result.nets_routed,
        "nets_failed": result.nets_failed,
        "expansions": result.expansions,
        "tracks_added": result.tracks_added,
        "vias_added": result.vias_added,
        "stitch_vias_added": result.stitch_vias_added,
        "timings": {k: round(float(v), 4) for k, v in timings.items()},
        "astar_ms_per_kexp": (
            round(1000.0 * result.timings.get("astar_s", 0.0)
                  / max(result.expansions, 1) * 1000.0 / 1000.0, 3)
            if result.expansions else None
        ),
        "failed_reasons": dict(result.failed_reasons or {}),
        "top_nets": [
            {"net": n,
             "wall_s": round(s.get("wall_s", 0.0), 3),
             "expansions": s.get("expansions", 0),
             "ok": s.get("ok", False)}
            for n, s in top
        ],
    }


def run_once(model, net_names, params) -> tuple[dict, object]:
    t0 = time.perf_counter()
    result, _, _ = route_nets(model, list(net_names), set(), params=params)
    wall = time.perf_counter() - t0
    return summarize(result, wall), result


def median_runs(model, net_names, params, repeat: int) -> dict:
    runs = [run_once(model, net_names, params)[0] for _ in range(repeat)]
    walls = sorted(r["wall_s"] for r in runs)
    med = median(walls)
    best = min(runs, key=lambda r: abs(r["wall_s"] - med))
    keys: set[str] = set()
    for r in runs:
        keys.update(r["timings"])
    med_timings = {
        k: round(median([r["timings"].get(k, 0.0) for r in runs]), 4)
        for k in sorted(keys)
    }
    return {
        "repeat": repeat,
        "wall_s_each": walls,
        "wall_s_median": round(median(walls), 3),
        "representative": best,
        "timings_median": med_timings,
        "expansions_median": median([r["expansions"] for r in runs]),
    }


def warmup() -> None:
    m = board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))
    nets = routable_nets(m)
    if nets:
        route_nets(m, nets[:1], set(), params=bench_params())


def profile_move_reroute(board: str, move_mm: float, top_n: int = 20) -> str:
    model = board_model(parse_file(BOARDS[board]))
    fp, affected = pick_moved_footprint(model)
    moved = apply_deltas(model, {fp.uuid: (move_mm, 0.0, 0.0)})
    nets = [n for n in affected if n in set(routable_nets(moved))]
    pr = cProfile.Profile()
    pr.enable()
    run_once(moved, nets, bench_params())
    pr.disable()
    buf = io.StringIO()
    pstats.Stats(pr, stream=buf).sort_stats("cumulative").print_stats(top_n)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", default="both",
                    choices=["both", "tube111", "dccf"])
    ap.add_argument("--mode", default="both",
                    choices=["both", "full", "move-reroute"])
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--move-mm", type=float, default=3.0)
    ap.add_argument("--max-nets", type=int, default=0,
                    help="cap full-route net count (0 = all routable)")
    ap.add_argument("--profile", action="store_true",
                    help="cProfile one move-reroute run per board")
    ap.add_argument("--out", default="",
                    help="JSON path (default docs/benchmarks/routing_<date>.json)")
    args = ap.parse_args()

    boards = ["tube111", "dccf"] if args.board == "both" else [args.board]
    modes = (["full", "move-reroute"] if args.mode == "both"
             else [args.mode])

    print("warming up numba on minimal...", flush=True)
    warmup()
    params = bench_params()

    results: list[dict] = []
    for board in boards:
        model = board_model(parse_file(BOARDS[board]))
        all_nets = routable_nets(model)
        full_nets = (all_nets[:args.max_nets] if args.max_nets else all_nets)
        fp, affected = pick_moved_footprint(model)
        moved = apply_deltas(model, {fp.uuid: (args.move_mm, 0.0, 0.0)})
        affected_nets = [n for n in affected
                         if n in set(routable_nets(moved))]
        print(f"{board}: {len(all_nets)} routable nets; "
              f"move {fp.ref} ({fp.uuid[:8]}) by {args.move_mm}mm -> "
              f"{len(affected_nets)} affected nets {affected_nets}",
              flush=True)
        if "full" in modes:
            print(f"  full route x{args.repeat} ...", flush=True)
            full = median_runs(model, full_nets, params, args.repeat)
            full.update({"board": board, "mode": "full",
                         "n_nets": len(full_nets)})
            results.append(full)
            print(f"  full median {full['wall_s_median']}s "
                  f"exp {full['expansions_median']}", flush=True)
        if "move-reroute" in modes:
            print(f"  move-reroute x{args.repeat} ...", flush=True)
            reroute = median_runs(moved, affected_nets, params,
                                  args.repeat)
            reroute.update({"board": board, "mode": "move-reroute",
                            "n_nets": len(affected_nets),
                            "moved_ref": fp.ref,
                            "affected_nets": affected_nets})
            results.append(reroute)
            print(f"  reroute median {reroute['wall_s_median']}s "
                  f"exp {reroute['expansions_median']}", flush=True)
        if args.profile:
            print(f"--- cProfile move-reroute {board} ---")
            print(profile_move_reroute(board, args.move_mm))

    out = (Path(args.out) if args.out else
           REPO / "docs" / "benchmarks" / f"routing_{date.today()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "params": params.to_dict(),
            "move_mm": args.move_mm,
            "repeat": args.repeat,
            "max_nets": args.max_nets,
        },
        "results": results,
    }
    out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
