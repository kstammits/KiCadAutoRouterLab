#!/usr/bin/env python3
"""Beads-scale triage: bounded routing measurements on /tmp/beads-2m.kicad_pcb.

Laptop-safe by construction: 0.2mm grid, fixed stages timed once, only
2-4 terminal signal nets routed (5 smallest), tight expansion caps, one
bounded mid-size probe (+3V3_A). Nets over max_terminals_per_net are
asserted fail-fast, never routed. Output JSON goes to /tmp (not
docs/benchmarks/).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import board_model, is_ground_net  # noqa: E402
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
    _terminals_for_net,
    resolve_clearance,
    resolve_track_width,
    route_nets,
)
from kicad_autorouter.routing.router import SECONDARY_BLOCKED  # noqa: E402
from kicad_autorouter.routing.router import build_via_ok_mask  # noqa: E402
from kicad_autorouter.routing.obstacles import via_halo_radius_cells  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402

POWER_NAMES = {"VIN", "+12V", "-12V", "VDD", "VCC", "VEE", "+5V", "-5V", "+3V3"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--board", default="/tmp/beads-2m.kicad_pcb")
    ap.add_argument("--resolution", type=float, default=0.2)
    ap.add_argument("--budget", type=int, default=100_000)
    ap.add_argument("--nets", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    t0 = time.perf_counter()
    model = board_model(parse_file(Path(args.board)))
    print(f"board: {len(model.footprints)} fps, {len(model.nets)} nets, "
          f"{sum(len(f.pads) for f in model.footprints)} pads", flush=True)

    params = RoutingParams(
        grid_resolution_mm=args.resolution,
        max_expansions=args.budget,
        max_attempts_per_net=1,
        stitch_gnd_vias=False,
        run_drc=False,
    )
    grid = create_grid_from_model(model, params.grid_resolution_mm, params.margin_mm)
    cells_m = len(grid.layers) * grid.width_cells * grid.height_cells / 1e6
    print(f"grid: {grid.width_cells}x{grid.height_cells}x{len(grid.layers)} = "
          f"{cells_m:.2f}M cells @ {args.resolution}mm", flush=True)

    # Terminal census (cheap: no fanout yet).
    census = []
    for net_name, conns in model.nets.items():
        try:
            terms = _terminals_for_net(model, grid, net_name)
        except Exception as e:  # noqa: BLE001 - triage must not die on one net
            census.append({"net": net_name, "pads": len(conns), "terminals": -1,
                           "error": str(e)[:120]})
            continue
        census.append({"net": net_name, "pads": len(conns),
                       "terminals": len(terms)})
    census.sort(key=lambda d: (d["terminals"] < 0, d["terminals"]))
    over = [c for c in census if c["terminals"] > params.max_terminals_per_net]
    print(f"terminal census: {len(census)} nets, "
          f"{len(over)} over cap({params.max_terminals_per_net}): "
          f"{[(c['net'], c['terminals']) for c in over[:8]]}", flush=True)

    # Fixed stages, timed once.
    t = time.perf_counter()
    cost_grid = build_occupancy_grid(model, grid, default_clearance_mm=params.clearance_mm)
    occupancy_s = time.perf_counter() - t
    blocked_frac = float((cost_grid >= 100).mean())
    occupied_frac = float((cost_grid > 0).mean())
    t = time.perf_counter()
    tht = _build_tht_via_mask(model, grid)
    tht_s = time.perf_counter() - t
    t = time.perf_counter()
    vbm = build_via_block_mask(model, grid, clearance_mm=params.clearance_mm,
                               track_half_mm=params.track_width_mm / 2.0)
    vbm_s = time.perf_counter() - t
    print(f"fixed: occupancy={occupancy_s:.3f}s tht={tht_s:.3f}s via_block={vbm_s:.3f}s "
          f"blocked={blocked_frac:.3f} occupied={occupied_frac:.3f}", flush=True)

    # Small signal nets only.
    cands = [c for c in census
             if 2 <= c["terminals"] <= 4
             and c["net"].upper() not in POWER_NAMES
             and not is_ground_net(c["net"])]
    cands.sort(key=lambda d: (d["terminals"], d["pads"], d["net"]))
    small = [c["net"] for c in cands[:args.nets]]
    print(f"small nets: {small}", flush=True)

    rows = []
    for net in small:
        tn = time.perf_counter()
        result, _, _ = route_nets(model, [net], set(), params=params,
                                  progress_cb=None, should_cancel=None)
        el = time.perf_counter() - tn
        r = {"net": net, "routed": result.nets_routed, "failed": result.nets_failed,
             "expansions": result.expansions, "wall_s": round(el, 3),
             "reasons": dict(result.failed_reasons)}
        rows.append(r)
        print(f"  {net}: routed={r['routed']} failed={r['failed']} "
              f"exp={r['expansions']} {el:.2f}s {r['reasons']}", flush=True)

    # One bounded mid-size probe (+3V3_A 27 terminals): tight per-search cap
    # plus a small per-net cap so it documents cost without hanging.
    probe = None
    mid = next((c for c in census if c["net"] == "+3V3_A"), None)
    if mid and 0 < mid["terminals"] <= params.max_terminals_per_net:
        p_params = RoutingParams(
            grid_resolution_mm=args.resolution,
            max_expansions=20_000,
            max_expansions_per_net=500_000,
            max_attempts_per_net=1,
            stitch_gnd_vias=False,
            run_drc=False,
        )
        tn = time.perf_counter()
        result, _, _ = route_nets(model, ["+3V3_A"], set(), params=p_params,
                                  progress_cb=None, should_cancel=None)
        el = time.perf_counter() - tn
        probe = {"net": "+3V3_A", "terminals": mid["terminals"],
                 "routed": result.nets_routed, "expansions": result.expansions,
                 "wall_s": round(el, 3), "reasons": dict(result.failed_reasons)}
        print(f"  probe {probe}", flush=True)

    out = Path(args.out) if args.out else Path(
        f"/tmp/beads_triage_{date.today().isoformat()}.json")
    out.write_text(json.dumps({
        "board": args.board, "resolution": args.resolution, "budget": args.budget,
        "grid_cells_M": round(cells_m, 2),
        "fixed_s": {"occupancy": round(occupancy_s, 3), "tht": round(tht_s, 3),
                    "via_block": round(vbm_s, 3)},
        "density": {"blocked_frac": blocked_frac, "occupied_frac": occupied_frac},
        "over_cap": over, "small_rows": rows, "mid_probe": probe,
        "triage_wall_s": round(time.perf_counter() - t0, 1),
    }, indent=2) + "\n")
    print(f"Saved {out} (wall {time.perf_counter()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
