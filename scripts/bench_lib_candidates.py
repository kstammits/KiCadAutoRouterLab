#!/usr/bin/env python3
"""Library-candidate microbenchmarks: hand-rolled vs library impls.

Covers the two cheapest Phase-1 candidates so the feature branch has
before/after numbers instead of opinions:
  - DSU (routing/dsu.py DisjointSet) vs networkx.utils.UnionFind
    (networkx import is optional; skipped when not installed)
  - mass._convex_hull_area (monotone chain) vs shapely MultiPoint.convex_hull

Usage:
    .venv/bin/python scripts/bench_lib_candidates.py [--out PATH]

Prints a table and saves JSON. Not part of the docs/benchmarks family
(one-off decision record, default /tmp/lib_candidates_<date>.json).
"""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import date
from pathlib import Path


def bench_dsu(n=2000, ops=20000, seed=0):
    from kicad_autorouter.routing.dsu import DisjointSet

    rng = random.Random(seed)
    pairs = [(rng.randrange(n), rng.randrange(n)) for _ in range(ops)]
    t0 = time.perf_counter()
    d = DisjointSet(range(n))
    for a, b in pairs:
        d.union(a, b)
    ours_s = time.perf_counter() - t0
    out = {"ours_s": ours_s, "ours_components": d.components, "n": n, "ops": ops}
    try:
        from networkx.utils import UnionFind as NXUF
    except ImportError:
        out["nx_s"] = None
        out["note"] = "networkx not installed; skipping"
        return out
    t0 = time.perf_counter()
    u = NXUF(range(n))
    for a, b in pairs:
        u.union(a, b)
    nx_s = time.perf_counter() - t0
    out["nx_s"] = nx_s
    out["nx_sets"] = len(list(u.to_sets()))
    out["speedup_ours_vs_nx"] = nx_s / ours_s if ours_s > 0 else None
    return out


def bench_hull(npts=200, reps=2000, seed=1):
    from shapely.geometry import MultiPoint

    from kicad_autorouter.mass import _convex_hull_area

    rng = random.Random(seed)
    pts = [(rng.uniform(0, 50), rng.uniform(0, 50)) for _ in range(npts)]
    t0 = time.perf_counter()
    for _ in range(reps):
        a1 = _convex_hull_area(pts)
    ours_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(reps):
        a2 = MultiPoint(pts).convex_hull.area
    shapely_s = time.perf_counter() - t0
    return {
        "ours_s": ours_s,
        "shapely_s": shapely_s,
        "ours_area": a1,
        "shapely_area": a2,
        "abs_diff": abs(a1 - a2),
        "speedup_ours_vs_shapely": shapely_s / ours_s if ours_s > 0 else None,
        "npts": npts,
        "reps": reps,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    dsu = bench_dsu()
    hull = bench_hull()
    nx_s = dsu.get("nx_s")
    nx_txt = f"{nx_s:.3f}s" if nx_s else "n/a"
    sp = dsu.get("speedup_ours_vs_nx")
    print(f"DSU  ours={dsu['ours_s']:.3f}s nx={nx_txt} "
          f"speedup={(round(sp, 2) + 'x') if sp else 'n/a'} "
          f"(components={dsu['ours_components']})")
    print(f"HULL ours={hull['ours_s']:.3f}s shapely={hull['shapely_s']:.3f}s "
          f"speedup={hull['speedup_ours_vs_shapely']:.2f}x diff={hull['abs_diff']:.6f}")
    out = {"date": str(date.today()), "dsu": dsu, "hull": hull}
    path = Path(args.out) if args.out else Path(f"/tmp/lib_candidates_{date.today()}.json")
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
