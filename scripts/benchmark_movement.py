#!/usr/bin/env python3
"""Movement benchmark: timed placement runs on a handful of parts + cProfile.

Complements ``benchmark_placement.py`` (which fits per-iteration cost vs
part count): this script grabs a deterministic handful of footprints on
each fixture, moves only those (``movable_uuids`` — everything else stays
fixed), and reports wall time plus a cProfile hotspot table per board.

Usage (under the project .venv)::

    .venv/bin/python scripts/benchmark_movement.py [--quick] [--out PATH]

--quick runs a CI smoke (minimal + small subsets). Results print as tables
and are saved as JSON (default
docs/benchmarks/placement_motion_<date>.json) for future planning.
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

from kicad_autorouter.board_model import board_model  # noqa: E402
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# Fixed iteration budget for timing: isolates per-step cost from
# convergence variance (a tiny eps forces the full budget).
TIMING_BUDGET = 30
TIMING_EPS = 1e-12
TIMING_EPS_SI = 1e-15  # meters: never converges, times the full budget
PROFILE_BUDGET = 10
PROFILE_TOP_N = 25


def load_board(fixture_name: str):
    """Load a board fixture (minimal, tube111, or DCCF.sved)."""
    path = FIXTURES / f"{fixture_name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{fixture_name}.sved.kicad_pcb"
    return board_model(parse_file(path))


def pick_parts(model, k: int) -> list[str]:
    """Deterministic handful: first ``k`` *movable* footprint UUIDs.

    Only unlocked footprints with >= 2 pads can move (locked parts,
    mounting holes, and single-pad test points feel no force: friction
    zeroes them and the sim converges at iteration 0). Sorted by ref
    for determinism (uuid fallback when ref is empty).
    """
    fps = sorted(
        (fp for fp in model.footprints
         if not getattr(fp, "locked", False) and len(fp.pads) >= 2),
        key=lambda fp: (fp.ref, fp.uuid),
    )
    return [fp.uuid for fp in fps[:k]]


def refs_for(model, uuids) -> list[str]:
    """Ref designators for logging (uuids -> refs, uuid fallback)."""
    by_uuid = {fp.uuid: fp for fp in model.footprints}
    out = []
    for u in uuids:
        fp = by_uuid[u]
        out.append(fp.ref if fp.ref else f"#{fp.uuid[:8]}")
    return out


def time_case(board_name: str, model, uuids: list[str],
              budget: int = TIMING_BUDGET) -> dict:
    """Time a fixed-budget movement run on the given parts."""
    params = PlacementParams(
        max_iterations=budget, convergence_eps_m=TIMING_EPS_SI
    )
    t0 = time.perf_counter()
    proposal = run_placement(model, params, movable_uuids=set(uuids))
    elapsed_s = time.perf_counter() - t0
    iters = max(proposal.iterations, 1)
    return {
        "board": board_name,
        "footprints": len(model.footprints),
        "moved_parts": len(uuids),
        "refs": refs_for(model, uuids),
        "iterations": proposal.iterations,
        "total_s": round(elapsed_s, 3),
        "ms_per_iter": round(elapsed_s / iters * 1000, 3),
        "deltas": len(proposal.deltas),
    }


def profile_case(board_name: str, model, uuids: list[str],
                 budget: int = PROFILE_BUDGET,
                 top_n: int = PROFILE_TOP_N) -> dict:
    """cProfile a short movement run; top entries by total + cumulative."""
    params = PlacementParams(
        max_iterations=budget, convergence_eps_m=TIMING_EPS_SI
    )
    pr = cProfile.Profile()
    pr.enable()
    run_placement(model, params, movable_uuids=set(uuids))
    pr.disable()
    stats = pstats.Stats(pr).sort_stats("tottime")

    def entries(sort_key: str) -> list[dict]:
        s = pstats.Stats(pr).sort_stats(sort_key)
        out = []
        for func, (cc, nc, tt, ct, _callers) in s.stats.items():
            out.append({
                "func": f"{func[0].split('/')[-1]}:{func[1]}:{func[2]}",
                "calls": cc,
                "tottime": round(tt, 4),
                "cumtime": round(ct, 4),
            })
        if sort_key == "tottime":
            out.sort(key=lambda d: -d["tottime"])
        else:
            out.sort(key=lambda d: -d["cumtime"])
        return out[:top_n]

    return {
        "board": board_name,
        "moved_parts": len(uuids),
        "refs": refs_for(model, uuids),
        "budget": budget,
        "by_tottime": entries("tottime"),
        "by_cumtime": entries("cumulative"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="CI smoke: few cases")
    ap.add_argument("--out", default=None, help="results JSON path")
    ap.add_argument("--parts", type=int, nargs="+", default=None,
                    help="handful sizes (default per plan)")
    args = ap.parse_args()

    if args.quick:
        plan = [("tube111", [4]), ("DCCF", [4])]
    elif args.parts:
        plan = [("tube111", args.parts), ("DCCF", args.parts)]
    else:
        plan = [("tube111", [1, 4, 16]), ("DCCF", [1, 4, 16])]

    rows = []
    for board_name, counts in plan:
        model = load_board(board_name)
        for k in counts:
            uuids = pick_parts(model, k)
            if not uuids:
                print(f"NOTE: {board_name} has no movable parts "
                      f"(skipping {k}-part case)")
                continue
            rows.append(time_case(board_name, model, uuids))

    print(f"{'board':<10}{'parts':>7}{'iters':>7}{'total_s':>9}"
          f"{'ms/iter':>10}{'deltas':>8}  refs")
    for r in rows:
        print(f"{r['board']:<10}{r['moved_parts']:>7}{r['iterations']:>7}"
              f"{r['total_s']:>9.3f}{r['ms_per_iter']:>10.3f}"
              f"{r['deltas']:>8}  {','.join(r['refs'][:6])}"
              f"{'…' if len(r['refs']) > 6 else ''}")

    # cProfile one handful per board (largest handful in the plan).
    profiles = []
    for board_name in dict.fromkeys(b for b, _ in plan):
        model = load_board(board_name)
        counts = next(c for b, c in plan if b == board_name)
        uuids = pick_parts(model, max(counts))
        if not uuids:
            continue
        prof = profile_case(board_name, model, uuids)
        profiles.append(prof)
        print(f"\ncProfile top by tottime "
              f"({board_name}, {len(uuids)} parts, {prof['budget']} iters):")
        for e in prof["by_tottime"][:12]:
            print(f"  {e['tottime']:>9.4f}s tot  {e['cumtime']:>9.4f}s cum  "
                  f"{e['calls']:>8} calls  {e['func']}")

    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks"
        / f"placement_motion_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"timing_budget": TIMING_BUDGET, "profile_budget": PROFILE_BUDGET,
         "cases": rows, "profiles": profiles}, indent=2) + "\n")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
