"""Placement benchmark: full place vs scatter+place vs displace-one re-place.

Measures wall-time and outcome quality of the force-spring placement
workflows (the interactive place loop), mirroring scripts/benchmark_routing.py
conventions (median of repeats, JSON output).

Usage:
    .venv/bin/python scripts/benchmark_placement.py \
        --board both --mode all --repeat 3 \
        --out docs/benchmarks/placement_YYYY-MM-DD.json

Scenarios (per board):
    filed       run_placement on filed positions, all movable
    scatter     deterministically scatter unlocked parts inside the
                footprint bbox (seeded), then run_placement (hard case)
    displace    move the highest-pad-count unlocked footprint by
                --move-mm, then run_placement (incremental loop)

Metrics per run: wall_s, iterations, stop_reason, residual_max_force_N,
final_max_disp_mm, n_moved, legality (is_legal, overlap/wall counts +
maxima), depenetrated, skipped_uuidless.
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
from kicad_autorouter.placement.params import PlacementParams  # noqa: E402
from kicad_autorouter.placement.runner import run_placement  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"
BOARDS = {
    "tube111": FIXTURES / "tube111.kicad_pcb",
    "dccf": FIXTURES / "DCCF.sved.kicad_pcb",
}


def movable_footprints(model):
    return [fp for fp in model.footprints if not fp.locked and fp.uuid]


def summarize(proposal, wall_s: float) -> dict:
    return {
        "wall_s": round(wall_s, 3),
        "iterations": proposal.iterations,
        "stop_reason": proposal.stop_reason,
        "residual_max_force_N": proposal.residual_max_force_N,
        "final_max_disp_mm": round(proposal.final_max_disp_mm, 4),
        "n_moved": len(proposal.deltas),
        "depenetrated": bool(proposal.depenetrated),
        "skipped_uuidless": proposal.skipped_uuidless,
        "legality_is_legal": bool(proposal.legality_is_legal),
        "n_overlapping_pairs": len(proposal.overlapping_pairs or ()),
        "n_wall_violations": len(proposal.wall_violations or ()),
        "max_pair_overlap_mm2": round(proposal.max_pair_overlap_mm2, 4),
        "max_wall_penetration_mm": round(proposal.max_wall_penetration_mm, 4),
    }


def run_once(model, params) -> dict:
    t0 = time.perf_counter()
    proposal = run_placement(model, params)
    wall = time.perf_counter() - t0
    return summarize(proposal, wall)


def median_runs(model, params, repeat: int) -> dict:
    runs = [run_once(model, params) for _ in range(repeat)]
    walls = sorted(r["wall_s"] for r in runs)
    med = median(walls)
    best = min(runs, key=lambda r: abs(r["wall_s"] - med))
    stops: dict[str, int] = {}
    for r in runs:
        stops[r["stop_reason"]] = stops.get(r["stop_reason"], 0) + 1
    return {
        "repeat": repeat,
        "wall_s_each": walls,
        "wall_s_median": round(med, 3),
        "stop_reasons": stops,
        "representative": best,
    }


def scatter_model(model, seed: int = 7, span_frac: float = 0.5):
    """Deterministic scatter of unlocked parts inside the footprint bbox."""
    import random
    movable = movable_footprints(model)
    xs = [fp.x_mm for fp in movable]
    ys = [fp.y_mm for fp in movable]
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    sx, sy = (max(xs) - min(xs)) * span_frac, (max(ys) - min(ys)) * span_frac
    rng = random.Random(seed)
    deltas = {}
    for fp in movable:
        deltas[fp.uuid] = (
            cx + rng.uniform(-sx, sx) - fp.x_mm,
            cy + rng.uniform(-sy, sy) - fp.y_mm,
            0.0,
        )
    return apply_deltas(model, deltas)


def pick_moved_footprint(model):
    best, best_nets = None, set()
    for fp in movable_footprints(model):
        nets = {
            net for net, conns in model.nets.items()
            for c in conns if c.ref == fp.ref
        }
        if len(nets) > len(best_nets):
            best, best_nets = fp, nets
    return best


def profile_run(model, params, top_n: int = 25) -> str:
    pr = cProfile.Profile()
    pr.enable()
    run_placement(model, params)
    pr.disable()
    buf = io.StringIO()
    pstats.Stats(pr, stream=buf).sort_stats("cumulative").print_stats(top_n)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", default="both",
                    choices=["both", "tube111", "dccf"])
    ap.add_argument("--mode", default="all",
                    choices=["all", "filed", "scatter", "displace"])
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--max-iterations", type=int, default=1000)
    ap.add_argument("--move-mm", type=float, default=3.0)
    ap.add_argument("--scatter-seed", type=int, default=7)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    boards = ["tube111", "dccf"] if args.board == "both" else [args.board]
    modes = (["filed", "scatter", "displace"] if args.mode == "all"
             else [args.mode])
    params = PlacementParams(max_iterations=args.max_iterations)

    results: list[dict] = []
    for board in boards:
        model = board_model(parse_file(BOARDS[board]))
        n_movable = len(movable_footprints(model))
        print(f"{board}: {len(model.footprints)} footprints, "
              f"{n_movable} movable", flush=True)
        if "filed" in modes:
            print(f"  filed x{args.repeat} ...", flush=True)
            r = median_runs(model, params, args.repeat)
            r.update({"board": board, "mode": "filed"})
            results.append(r)
            print(f"  filed median {r['wall_s_median']}s "
                  f"stops {r['stop_reasons']}", flush=True)
        if "scatter" in modes:
            scattered = scatter_model(model, seed=args.scatter_seed)
            print(f"  scatter+place x{args.repeat} ...", flush=True)
            r = median_runs(scattered, params, args.repeat)
            r.update({"board": board, "mode": "scatter",
                      "scatter_seed": args.scatter_seed})
            results.append(r)
            print(f"  scatter median {r['wall_s_median']}s "
                  f"stops {r['stop_reasons']}", flush=True)
        if "displace" in modes:
            fp = pick_moved_footprint(model)
            moved = apply_deltas(model, {fp.uuid: (args.move_mm, 0.0, 0.0)})
            print(f"  displace {fp.ref} +{args.move_mm}mm + "
                  f"re-place x{args.repeat} ...", flush=True)
            r = median_runs(moved, params, args.repeat)
            r.update({"board": board, "mode": "displace",
                      "moved_ref": fp.ref, "move_mm": args.move_mm})
            results.append(r)
            print(f"  displace median {r['wall_s_median']}s "
                  f"stops {r['stop_reasons']}", flush=True)
        if args.profile:
            print(f"--- cProfile filed {board} ---")
            print(profile_run(model, params))

    out = (Path(args.out) if args.out else
           REPO / "docs" / "benchmarks" / f"placement_{date.today()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "params": params.to_dict(),
            "repeat": args.repeat,
            "move_mm": args.move_mm,
            "scatter_seed": args.scatter_seed,
        },
        "results": results,
    }
    out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
