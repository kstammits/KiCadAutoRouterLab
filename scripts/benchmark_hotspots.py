#!/usr/bin/env python3
"""Hotspot benchmark: production-tail stages skipped by other benchmarks.

Covers the engine end-to-end gaps, not the already-profiled micros:
  1. finish tail — route_nets vs refine_routing (seeds 1 vs 3) vs
     polish_routes, with the raw timings dict reported verbatim so a
     dominating stage (smooth/biarc, refine trials, stitch, DRC,
     writeback) shows up directly;
  2. scale/cache/chunk — ObstacleCache fresh vs reused, plus the largest
     net per board (chunked/piecemeal path) at a bounded expansion cap;
  3. placement tail — fixed-budget run_placement split into sim vs
     check_legality, plus one short cProfile per board.

Bounded for a 2-5 minute full run; --quick is a <60s CI smoke.

Usage (under the project .venv)::

    .venv/bin/python scripts/benchmark_hotspots.py [--quick] [--out PATH]

Results print as tables and are saved as JSON (default
docs/benchmarks/hotspots_<date>.json), same shape as the other
benchmark families: {"params", "cases", "profile_top"}.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import (  # noqa: E402
    board_model,
    is_ground_net,
)
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402
from kicad_autorouter.placement.legality import check_legality  # noqa: E402
from kicad_autorouter.routing.obstacles import ObstacleCache  # noqa: E402
from kicad_autorouter.routing.pipeline import (  # noqa: E402
    RoutingParams,
    refine_routing,
    route_nets,
)
from kicad_autorouter.routing.polish import polish_routes  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

POWER_NAMES = {"VIN", "+12V", "-12V", "VDD", "VCC", "VEE", "+5V", "-5V", "+3V3"}
TIMING_BUDGET = 50
TIMING_EPS_SI = 1e-15  # meters: never converges, times the full budget
PROFILE_TOP_N = 15


def load_board(name: str):
    path = FIXTURES / f"{name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{name}.sved.kicad_pcb"
    return board_model(parse_file(path))


def pick_signal_nets(model, k: int) -> list[str]:
    cands = [
        (len(conns), name)
        for name, conns in model.nets.items()
        if 2 <= len(conns) <= 4
        and name.upper() not in POWER_NAMES
        and not is_ground_net(name)
    ]
    cands.sort()
    return [name for _, name in cands[:k]]


def pick_largest_net(model) -> str | None:
    """Largest routable net by pad count (exercises the chunked path)."""
    best, best_n = None, 0
    for name, conns in model.nets.items():
        if name.upper() in POWER_NAMES or is_ground_net(name):
            continue
        if len(conns) > best_n:
            best, best_n = name, len(conns)
    return best


def time_route_tail(
    label: str, board: str, nets: list[str], params: RoutingParams,
    cache=None, refine_seeds: int | None = None,
    polish_effort: float | None = None,
) -> dict:
    """One end-to-end row: route (+ optional refine + optional polish)."""
    model = load_board(board)
    t0 = time.perf_counter()
    if refine_seeds is not None:
        from dataclasses import replace

        result, _, grid = refine_routing(
            model, nets, set(),
            params=replace(params, refine_seeds=refine_seeds),
            obstacle_cache=cache,
        )
        mode = f"refine{refine_seeds}"
    else:
        result, _, grid = route_nets(
            model, nets, set(), params=params, obstacle_cache=cache,
        )
        mode = "route"
    wall = time.perf_counter() - t0
    t = dict(result.timings)
    row: dict = {
        "case": label,
        "board": board,
        "mode": mode,
        "nets": len(nets),
        "net_names": nets,
        "nets_routed": result.nets_routed,
        "nets_failed": result.nets_failed,
        "expansions": result.expansions,
        "wall_s": round(wall, 3),
        "route_s": round(t.get("route_s", 0.0), 3),
        "astar_s": round(t.get("astar_s", 0.0), 3),
        "smooth_s": round(t.get("smooth_s", 0.0), 3),
        "occupancy_s": round(t.get("occupancy_s", 0.0), 3),
        "grid_build_s": round(t.get("grid_build_s", 0.0), 3),
        "total_s": round(t.get("total_s", wall), 3),
        "arcs": result.arcs_added,
        "wire_mm": round(result.total_wire_mm, 2),
    }
    if polish_effort is not None and getattr(result, "net_routes", None):
        tp = time.perf_counter()
        _, report = polish_routes(
            dict(result.net_routes), dict(result.width_map),
            model, grid, params, nets, polish_effort,
        )
        row["polish_s"] = round(time.perf_counter() - tp, 3)
        row["polished"] = report.get("polished", 0)
        row["wire_saved_mm"] = report.get("wire_saved_mm", 0.0)
    else:
        row["polish_s"] = 0.0
    return row


def time_cache_pair(board: str, nets: list[str], params: RoutingParams) -> dict:
    """Fresh ObstacleCache vs reused (the incremental production path)."""
    t0 = time.perf_counter()
    r1, _, _ = route_nets(
        load_board(board), nets, set(), params=params,
        obstacle_cache=ObstacleCache(),
    )
    fresh_s = time.perf_counter() - t0
    cache = ObstacleCache()
    route_nets(load_board(board), nets[:1], set(), params=params,
               obstacle_cache=cache)  # warm
    t0 = time.perf_counter()
    r2, _, _ = route_nets(
        load_board(board), nets, set(), params=params, obstacle_cache=cache,
    )
    reused_s = time.perf_counter() - t0
    occ1 = r1.timings.get("occupancy_s", 0.0)
    occ2 = r2.timings.get("occupancy_s", 0.0)
    return {
        "case": f"cache-{board}",
        "board": board,
        "nets": len(nets),
        "fresh_s": round(fresh_s, 3),
        "reused_s": round(reused_s, 3),
        "fresh_occupancy_s": round(occ1, 3),
        "reused_occupancy_s": round(occ2, 3),
        "speedup": round(fresh_s / max(reused_s, 1e-9), 2),
    }


def time_placement_tail(board: str) -> dict:
    """Fixed-budget sim vs post-hoc legality check (the placement tail)."""
    model = load_board(board)
    params = PlacementParams(
        max_iterations=TIMING_BUDGET, convergence_eps_m=TIMING_EPS_SI)
    t0 = time.perf_counter()
    proposal = run_placement(model, params)
    sim_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    report = check_legality(model, proposal.deltas)
    legal_s = time.perf_counter() - t0
    return {
        "case": f"placement-{board}",
        "board": board,
        "footprints": len(model.footprints),
        "iterations": proposal.iterations,
        "sim_s": round(sim_s, 3),
        "ms_per_iter": round(sim_s / max(proposal.iterations, 1) * 1000, 3),
        "legality_s": round(legal_s, 3),
        "legal": bool(report.is_legal),
        "max_overlap_mm2": round(report.max_pair_overlap_mm2, 3),
    }


def profile_board(board: str, top_n: int = PROFILE_TOP_N) -> list[dict]:
    """One short cProfile mixing placement sim + a 1-net route."""
    model = load_board(board)
    nets = pick_signal_nets(model, 1)
    params = RoutingParams(max_expansions=50_000, max_attempts_per_net=1,
                           stitch_gnd_vias=False, run_drc=False)
    pparams = PlacementParams(max_iterations=10,
                              convergence_eps_m=TIMING_EPS_SI)
    pr = cProfile.Profile()
    pr.enable()
    run_placement(model, pparams)
    if nets:
        route_nets(model, nets, set(), params=params)
    pr.disable()
    buf = io.StringIO()
    pstats.Stats(pr, stream=buf).sort_stats("cumulative").print_stats(top_n)
    out = []
    for func, (_cc, _nc, tt, ct, _callers) in pstats.Stats(pr).stats.items():
        out.append({
            "func": f"{func[0].split('/')[-1]}:{func[1]}:{func[2]}",
            "tottime": round(tt, 4),
            "cumtime": round(ct, 4),
        })
    out.sort(key=lambda d: -d["cumtime"])
    return out[:top_n]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="CI smoke: few nets")
    ap.add_argument("--out", default=None, help="results JSON path")
    args = ap.parse_args()

    base = RoutingParams(max_attempts_per_net=1, stitch_gnd_vias=False,
                         run_drc=False)
    if args.quick:
        tail_plan = [("DCCF", 2), ("tube111", 1)]
        cache_plan = [("DCCF", 2)]
        place_plan = ["tube111"]
        profile_plan = ["tube111"]
    else:
        tail_plan = [("DCCF", 3), ("tube111", 3)]
        cache_plan = [("DCCF", 3), ("tube111", 3)]
        place_plan = ["tube111", "DCCF"]
        profile_plan = ["tube111", "DCCF"]

    cases: list[dict] = []
    # 1. finish tail: same nets via route / refine1 / refine3 + polish probe.
    for board, k in tail_plan:
        nets = pick_signal_nets(load_board(board), k)
        if not nets:
            continue
        cases.append(time_route_tail(f"{board}x{k}-route", board, nets,
                                     base, polish_effort=3.0))
        cases.append(time_route_tail(f"{board}x{k}-refine3", board, nets,
                                     base, refine_seeds=3))

    # 2. largest net per board (chunked-path probe, bounded cap).
    for board in dict.fromkeys(b for b, _ in tail_plan):
        big = pick_largest_net(load_board(board))
        if big:
            capped = RoutingParams(max_attempts_per_net=1,
                                   stitch_gnd_vias=False, run_drc=False,
                                   max_expansions=100_000)
            cases.append(time_route_tail(f"{board}-largest", board, [big],
                                         capped))

    # 3. cache fresh vs reused.
    cache_rows = [time_cache_pair(b, pick_signal_nets(load_board(b), k), base)
                  for b, k in cache_plan
                  if pick_signal_nets(load_board(b), k)]

    # 4. placement tail.
    place_rows = [time_placement_tail(b) for b in place_plan]
    profiles = {b: profile_board(b) for b in profile_plan}

    print(f"{'case':<20}{'mode':>8}{'nets':>6}{'routed':>7}{'failed':>7}"
          f"{'expans':>9}{'wall_s':>9}{'astar':>8}{'smooth':>8}"
          f"{'polish':>8}{'refine?':>8}")
    for r in cases:
        print(f"{r['case']:<20}{r['mode']:>8}{r['nets']:>6}"
              f"{r['nets_routed']:>7}{r['nets_failed']:>7}"
              f"{r['expansions']:>9}{r['wall_s']:>9.3f}"
              f"{r['route_s']:>8.3f}{r['smooth_s']:>8.3f}"
              f"{r['polish_s']:>8.3f}{'':>8}")
    print("\ncache (fresh vs reused ObstacleCache):")
    for r in cache_rows:
        print(f"  {r['case']:<16}nets={r['nets']} "
              f"fresh={r['fresh_s']:.3f}s (occ {r['fresh_occupancy_s']:.3f}s) "
              f"reused={r['reused_s']:.3f}s (occ {r['reused_occupancy_s']:.3f}s) "
              f"speedup={r['speedup']}x")
    print("\nplacement tail (sim vs legality):")
    for r in place_rows:
        print(f"  {r['case']:<18}iters={r['iterations']} "
              f"sim={r['sim_s']:.3f}s ({r['ms_per_iter']:.2f}ms/it) "
              f"legality={r['legality_s']:.3f}s legal={r['legal']}")
    for b, prof in profiles.items():
        print(f"\nprofile top ({b}, sim + 1-net route, cumulative):")
        for e in prof[:8]:
            print(f"  {e['cumtime']:>9.4f}s cum  {e['tottime']:>9.4f}s tot  "
                  f"{e['func']}")

    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks"
        / f"hotspots_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"params": base.to_dict(), "cases": cases,
         "cache": cache_rows, "placement": place_rows,
         "profiles": profiles}, indent=2) + "\n")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
