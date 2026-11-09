#!/usr/bin/env python3
"""Sweep label clearance on a synthetic facing-label pair.

Compares ``label_clearance_m`` values (orbit always on, single spring
tether): final label offsets, orbit-radius usage, stop reason,
iterations, and wall-clock. One command for the clearance experiment —
run under the project .venv::

    .venv/bin/python scripts/sweep_label_clearance.py [--iters N] [--json-out PATH]

The pair is deliberately uncomfortable: two netted parts 4 mm apart with
big facing labels, so contact ejects the labels and the clearance
decides where they settle. See ``docs/roadmap.md`` (label orbit).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from helpers import make_board, make_fp  # noqa: E402
from kicad_autorouter.board_model import SilkLabel  # noqa: E402
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402


def _lab(ref: str, lx: float, w_mm: float = 6.0) -> SilkLabel:
    return SilkLabel(
        text="REFDES",
        local_x_mm=lx,
        local_y_mm=0.0,
        local_angle_deg=0.0,
        layer="F.SilkS",
        size_mm=(w_mm / (6 * 0.6), 1.0),
        thickness_mm=0.15,
        uuid=f"{ref}-label",
    )


def _pair(label_w_mm: float = 6.0, gap_mm: float = 4.0):
    fa = replace(
        make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=1.5, hh=1.0),
        label=_lab("A", 2.0, label_w_mm),
    )
    fb = replace(
        make_fp("B", "uuid-b", gap_mm, 0.0, ("N1", "N3"), hw=1.5, hh=1.0),
        label=_lab("B", -2.0, label_w_mm),
    )
    model = make_board([fa, fb])
    return replace(model, nets={"N1": (), "N2": (), "N3": ()})


def _summarize(model, params: PlacementParams) -> dict:
    t0 = time.perf_counter()
    prop = run_placement(model, params)
    dt = time.perf_counter() - t0
    offs = {}
    max_r = 0.0
    for uuid, (dx, dy) in (prop.label_deltas or {}).items():
        r = math.hypot(dx, dy)
        max_r = max(max_r, r)
        offs[uuid] = [round(dx, 3), round(dy, 3), round(r, 3)]
    return {
        "clearance_m": params.label_clearance_m,
        "stop": prop.stop_reason,
        "iterations": prop.iterations,
        "label_offsets": offs,
        "max_radius_mm": round(max_r, 3),
        "seconds": round(dt, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--label-w", type=float, default=6.0)
    ap.add_argument("--gap", type=float, default=4.0)
    ap.add_argument("--wall-halo", type=float, default=0.002)
    ap.add_argument("--clearances", type=float, nargs="*", default=[0.0, 0.001, 0.003, 0.005])
    ap.add_argument("--json-out", type=str, default="")
    args = ap.parse_args()

    base = {"max_iterations": args.iters, "wall_halo_m": args.wall_halo}
    model = _pair(label_w_mm=args.label_w, gap_mm=args.gap)
    rows = []
    for clearance in args.clearances:
        rows.append(_summarize(
            model, PlacementParams(**base, label_clearance_m=clearance)))
    print(f"{'clear_mm':<10}{'stop':<10}{'iters':>7}{'max_r':>8}{'secs':>8}  offsets")
    for r in rows:
        print(f"{r['clearance_m'] * 1000.0:<10.1f}"
              f"{r['stop']:<10}{r['iterations']:>7}{r['max_radius_mm']:>8}"
              f"{r['seconds']:>8}  {r['label_offsets']}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
