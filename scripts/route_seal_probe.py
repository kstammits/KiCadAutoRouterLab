#!/usr/bin/env python3
"""Seal probe: route a handful of tight/control pairs across halo/weight
cells and record per-terminal seal facts + per-route cost.

Laptop-bounded: budget 100k/search, retries 2, ~9 pairs x 4 cells.
Output JSON to /tmp (not docs/benchmarks/).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

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
    _rip_up_nets_from_model,
    _terminals_for_net,
    resolve_clearance,
    resolve_track_width,
    via_halo_radius_cells,
)
from kicad_autorouter.routing.router import (  # noqa: E402
    SECONDARY_BLOCKED,
    CostMap,
    SingleNetRouter,
)
from kicad_autorouter.sexpr import parse_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
BUDGET = 100_000
RETRIES = 2


def load_board(name: str):
    if name == "beads":
        return board_model(parse_file(Path("/tmp/beads-2m.kicad_pcb")))
    path = FIXTURES / f"{name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{name}.sved.kicad_pcb"
    return board_model(parse_file(path))


def seal_facts(secondary: np.ndarray, vbm: np.ndarray,
               terms: list) -> list[dict]:
    out = []
    for i, (c, r, lyr) in enumerate(terms):
        out.append({"idx": i, "cell": [c, r, lyr],
                    "own_secondary": int(secondary[lyr, r, c]),
                    "via_blocked": bool(vbm[r, c])})
    return out


def run_cell(grid, cost_grid, secondary, terms, weight: float,
             halo: float, base_secondary: np.ndarray, tht, vbm,
             via_radius: int, label: str, config: str) -> dict:
    sec = base_secondary if halo == 1.0 else scale_soft(base_secondary, halo)
    router = SingleNetRouter(
        grid, cost_grid, CostMap(heuristic_weight=weight),
        tht, vbm, max_expansions=BUDGET, window_retries=RETRIES)
    router.via_clearance_cells = via_radius
    t0 = time.perf_counter()
    result = router.route(terms, sec)
    el = time.perf_counter() - t0
    return {"case": label, "config": config, "pairs": len(terms),
            "success": bool(result.success), "expansions": result.expansions,
            "time_s": round(el, 3), "cost": round(result.cost, 1),
            "path_len": len(result.path)}


def scale_soft(secondary: np.ndarray, factor: float) -> np.ndarray:
    out = secondary.copy()
    soft = (out > 0) & (out < SECONDARY_BLOCKED)
    out[soft] = (out[soft].astype(np.float32) * factor).astype(out.dtype)
    return out


def build_net(model, grid, net: str, res: float):
    ripped = _rip_up_nets_from_model(model, [net])
    cost_grid = build_occupancy_grid(
        ripped, grid, default_clearance_mm=RoutingParams().clearance_mm)
    params = RoutingParams(grid_resolution_mm=res)
    width = resolve_track_width(model, params, net, ())
    clear = resolve_clearance(model, params, net)
    tht = _build_tht_via_mask(model, grid)
    vbm = build_via_block_mask(model, grid, clearance_mm=clear,
                               track_half_mm=width / 2.0)
    base = build_fanout_cost(model, grid, net, clearance_mm=clear,
                             track_half_mm=width / 2.0)
    via_radius = via_halo_radius_cells(params.via_size_mm, clear,
                                       width / 2.0, res)
    terms = _terminals_for_net(model, grid, net)
    return cost_grid, tht, vbm, base, via_radius, terms


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=None)
    ap.add_argument("--res", type=float, default=0.1)
    args = ap.parse_args()

    # (board, net, pair-pick): 'tight' = first tight terminal + terminal 0,
    # 'control' = first two non-tight terminals (falls back to 0,1).
    plan = [("tube111", "+12V"), ("tube111", "C2Output"),
            ("tube111", "grid_cv"), ("DCCF.sved", "VIN"),
            ("beads", "AUDIO_INL"), ("beads", "ASSIGN_LED_1")]
    sweep = [(3.0, 1.0), (1.0, 1.0), (3.0, 0.3), (1.0, 0.3)]

    dossiers, rows = [], []
    for board, net in plan:
        model = load_board(board)
        cost_grid, tht, vbm, base, via_radius, terms = build_net(
            model, create_grid_from_model(model, args.res, 2.0), net, args.res)
        grid = create_grid_from_model(model, args.res, 2.0)
        # Rebuild against the same grid object used for routing.
        cost_grid, tht, vbm, base, via_radius, terms = build_net(
            model, grid, net, args.res)
        tight_idx = [i for i, (c, r, lyr) in enumerate(terms)
                     if int(base[lyr, r, c]) >= 500]
        pairs = {}
        if tight_idx:
            ti = tight_idx[0]
            oi = 0 if ti != 0 else 1
            if len(terms) > oi:
                pairs["tight"] = [terms[ti], terms[oi]]
        lo = [t for i, t in enumerate(terms) if i not in tight_idx[:1]]
        if len(lo) >= 2:
            pairs["control"] = [lo[0], lo[1]]
        elif len(terms) >= 2:
            pairs["control"] = [terms[0], terms[1]]
        dossiers.append({"board": board, "net": net, "n_terminals": len(terms),
                         "tight_idx": tight_idx,
                         "seals": seal_facts(base, vbm, terms)})
        for kind, pair in pairs.items():
            for weight, halo in sweep:
                rows.append(run_cell(
                    grid, cost_grid, base, pair, weight, halo, base,
                    tht, vbm, via_radius, f"{board}:{net}:{kind}",
                    f"w={weight} halo={halo}"))
                print(f"  {rows[-1]['case']} {rows[-1]['config']} "
                      f"ok={rows[-1]['success']} exp={rows[-1]['expansions']} "
                      f"{rows[-1]['time_s']}s", flush=True)

    out = Path(args.out) if args.out else Path(
        f"/tmp/seal_probe_{date.today().isoformat()}.json")
    out.write_text(json.dumps(
        {"budget": BUDGET, "retries": RETRIES, "res": args.res,
         "dossiers": dossiers, "rows": rows}, indent=2) + "\n")
    print(f"Saved {out} ({len(rows)} route cells)")


if __name__ == "__main__":
    main()
