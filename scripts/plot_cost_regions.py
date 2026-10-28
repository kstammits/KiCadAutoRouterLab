#!/usr/bin/env python3
"""Banded cost-region heatmaps: occupancy + per-net secondary for beads vs
older fixtures. Answers "is this board just a terrible pile of parts?"

Bands (F.Cu/B.Cu panels): free=white, soft halo=yellow/orange,
hard ring (>=1000)=magenta, blocked occupancy=dark red hatch, own
terminals=green circles. Stats printed + saved alongside.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Circle

from kicad_autorouter.board_model import board_model
from kicad_autorouter.routing import build_occupancy_grid, create_grid_from_model
from kicad_autorouter.routing.obstacles import build_fanout_cost
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    _terminals_for_net,
    resolve_clearance,
    resolve_track_width,
)
from kicad_autorouter.routing.router import SECONDARY_BLOCKED
from kicad_autorouter.sexpr import parse_file

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def load_board(name: str):
    if name == "beads":
        return board_model(parse_file(Path("/tmp/beads-2m.kicad_pcb")))
    path = FIXTURES / f"{name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{name}.sved.kicad_pcb"
    return board_model(parse_file(path))


def analyze(board: str, net: str, res: float) -> dict:
    model = load_board(board)
    params = RoutingParams(grid_resolution_mm=res)
    grid = create_grid_from_model(model, res, params.margin_mm)
    occ = build_occupancy_grid(model, grid, default_clearance_mm=params.clearance_mm)
    width = resolve_track_width(model, params, net, ())
    clear = resolve_clearance(model, params, net)
    sec = build_fanout_cost(model, grid, net, clearance_mm=clear,
                            track_half_mm=width / 2.0)
    terms = _terminals_for_net(model, grid, net)
    H, W = grid.height_cells, grid.width_cells
    cells = len(grid.layers) * H * W
    stats = {"board": board, "net": net, "res": res,
             "grid": [W, H, len(grid.layers)],
             "cells_M": round(cells / 1e6, 3)}
    for li, layer in enumerate(grid.layers):
        o = occ[li]
        s = sec[li]
        stats[layer] = {
            "occ_blocked_frac": round(float((o >= 100).mean()), 4),
            "occ_tolled_frac": round(float(((o > 0) & (o < 100)).mean()), 4),
            "sec_soft_frac": round(float(((s > 0) & (s < SECONDARY_BLOCKED)).mean()), 4),
            "sec_hard_frac": round(float((s >= SECONDARY_BLOCKED).mean()), 4),
            "walkable_frac": round(float(((o < 100) & (s < SECONDARY_BLOCKED)).mean()), 4),
        }
    seals = []
    for i, (c, r, lyr) in enumerate(terms):
        seals.append({"idx": i, "secondary": int(sec[lyr, r, c])})
    stats["terminals"] = len(terms)
    stats["sealed_terminals"] = sum(1 for s in seals if s["secondary"] >= SECONDARY_BLOCKED)
    return {"model": model, "grid": grid, "occ": occ, "sec": sec,
            "terms": terms, "stats": stats}


def plot(result: dict, out: Path) -> None:
    grid, occ, sec, terms = result["grid"], result["occ"], result["sec"], result["terms"]
    stats, layers = result["stats"], grid.layers
    n = len(layers)
    fig, axes = plt.subplots(1, max(n, 1), figsize=(6 * max(n, 1), 6),
                             squeeze=False)
    for li, layer in enumerate(layers):
        ax = axes[0][li]
        o, s = occ[li], sec[li]
        img = np.zeros((*o.shape, 4))  # RGBA white base
        img[..., :3] = 1.0
        img[..., 3] = 1.0
        walk = (o < 100) & (s < SECONDARY_BLOCKED)
        img[walk] = (1.0, 1.0, 1.0, 1.0)
        soft = (s > 0) & (s < SECONDARY_BLOCKED)
        # intensity by value: outer(100)->light yellow, inner(500+)->orange
        v = np.clip(s[soft].astype(float) / 500.0, 0.15, 1.0)
        img[soft] = np.stack([np.ones_like(v), 1.0 - 0.45 * v,
                              np.zeros_like(v), np.full_like(v, 0.85)], axis=-1)
        hard = s >= SECONDARY_BLOCKED
        img[hard] = (0.85, 0.1, 0.85, 0.9)
        blocked = o >= 100
        img[blocked & ~hard] = (0.55, 0.05, 0.05, 0.95)
        ax.imshow(img, origin="upper")
        for i, (c, r, lyr) in enumerate(terms):
            if lyr == li:
                ax.add_patch(Circle((c, r), radius=6, facecolor="none",
                                    edgecolor="lime", linewidth=1.5))
                ax.text(c, r - 9, str(i), color="lime", fontsize=7,
                        ha="center", weight="bold")
        st = stats[layer]
        ax.set_title(f"{layer}: walkable={st['walkable_frac']:.2%} "
                     f"hard={st['sec_hard_frac']:.2%} soft={st['sec_soft_frac']:.2%} "
                     f"occ_blk={st['occ_blocked_frac']:.2%}", fontsize=9)
        ax.set_xticks([]); ax.set_yticks([])
    fig.suptitle(f"{stats['board']}:{stats['net']} secondary regions @ {stats['res']}mm "
                 f"({stats['terminals']} terms, {stats['sealed_terminals']} sealed) "
                 f"— magenta=hard ring, orange=soft halo, dark red=blocked, green=terminals")
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    print(f"Saved {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--res", type=float, default=0.2)
    ap.add_argument("--outdir", default="/tmp")
    args = ap.parse_args()
    plan = [("beads", "AUDIO_INL"), ("beads", "ASSIGN_LED_1"),
            ("tube111", "C2Output"), ("DCCF", "VIN")]
    summary = {}
    for board, net in plan:
        t0 = time.perf_counter()
        try:
            r = analyze(board, net, args.res)
        except Exception as e:  # noqa: BLE001
            print(f"{board}:{net} FAILED {e}")
            continue
        key = f"{board}:{net}"
        summary[key] = r["stats"]
        print(f"{key}: {json.dumps(r['stats'])} ({time.perf_counter()-t0:.1f}s)")
        plot(r, Path(args.outdir) / f"cost_regions_{board}_{net.replace('/', '_')}.png")
    Path(args.outdir, "cost_regions_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
