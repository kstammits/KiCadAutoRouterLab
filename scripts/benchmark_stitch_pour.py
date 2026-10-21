#!/usr/bin/env python3
"""Stitch+pour benchmark: GND continuity analysis + repair cost at scale.

Covers what ``benchmark_routing.py`` deliberately skips: the pour copper
model (``pour_copper_mask`` pullback/add-back/opening, ``ndimage.label``,
``pour_corridors`` EDT, ``severance_cost`` build) and the stitch planner
(``plan_stitches`` overlap-via siting + bridge A* + budgets).

Each case reports island counts before/after, vias/tracks placed, and
per-stage wall time so pour retunes (clearance, min_thickness, severance
weight), halo changes, or stitch budget changes compare apples-to-apples.

Usage (under the project .venv)::

    .venv/bin/python scripts/benchmark_stitch_pour.py [--quick] [--out PATH]

--quick runs a CI smoke (synthetics + tube111, single budget). Results
print as tables and are saved as JSON (default
docs/benchmarks/stitch_pour_<date>.json).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import (  # noqa: E402
    BoardModel,
    Track,
    Point,
    Zone,
)
from kicad_autorouter.routing.grid import create_grid_from_model  # noqa: E402
from kicad_autorouter.routing.pipeline import RoutingParams  # noqa: E402
from kicad_autorouter.routing.pour import (  # noqa: E402
    gnd_islands,
    labeled_copper,
    pour_corridors,
    pour_net_names,
    severance_cost,
)
from kicad_autorouter.routing.stitch import plan_stitches  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402
from kicad_autorouter.board_model import board_model  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from helpers import make_fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _rect_zone(uuid, net, layers=("F.Cu",), x0=0.0, y0=0.0, x1=40.0, y1=30.0,
               clearance=0.5, min_thickness=0.25):
    return Zone(
        uuid=uuid, layers=tuple(layers),
        polygon=(Point(x0, y0), Point(x1, y0), Point(x1, y1), Point(x0, y1)),
        net_name=net, clearance_mm=clearance, min_thickness_mm=min_thickness,
    )


def load_ghost_test():
    """Load ghost_test fixture which has ghost footprints."""
    path = FIXTURES / "ghost_test.kicad_pcb"
    return board_model(parse_file(path))


def _board(fps=(), zones=(), tracks=(), vias=(), edge=50.0):
    fps = tuple(fps)
    return BoardModel(
        footprints=fps,
        keepout_zones=(),
        zones=tuple(zones),
        tracks=tuple(tracks),
        vias=tuple(vias),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def _track(x1, y1, x2, y2, net, layer="F.Cu", width=0.25):
    return Track(start=Point(x1, y1), end=Point(x2, y2),
                 width_mm=width, layer=layer, net_name=net)


def synthetic_overlap():
    """Two-layer sever: foreign track splits F.Cu, intact B.Cu -> 1 via."""
    fp = make_fp("J1", "uuid-j1", 5.0, 15.0, ("GND",), tht=True)
    return _board(
        fps=[fp],
        zones=[_rect_zone("z1", "GND", ("F.Cu",)),
               _rect_zone("z2", "GND", ("B.Cu",))],
        tracks=[_track(20.0, -5.0, 20.0, 35.0, "SIG")],
    )


def synthetic_bridge():
    """Single-layer sever with a short detour available -> bridge track."""
    fp = make_fp("J1", "uuid-j1", 5.0, 6.0, ("GND",), tht=True)
    return _board(
        fps=[fp],
        zones=[_rect_zone("z1", "GND", ("F.Cu",), y1=12.0)],
        tracks=[_track(20.0, -5.0, 20.0, 17.0, "SIG")],
    )


def load_board(fixture_name: str):
    path = FIXTURES / f"{fixture_name}.kicad_pcb"
    if not path.exists():
        path = FIXTURES / f"{fixture_name}.sved.kicad_pcb"
    return board_model(parse_file(path))


def time_pour_stages(model, grid, params: RoutingParams) -> dict:
    """Time pour analysis stages; return timings + island facts."""
    nets = pour_net_names(model)
    # 1. labeled_copper (mask + ndimage.label) per pour net.
    t0 = time.perf_counter()
    label_counts = {}
    for net in nets:
        out = labeled_copper(model, grid, net)
        label_counts[net] = {layer: out[layer][2] for layer in out}
    label_s = time.perf_counter() - t0

    # 2. corridors EDT per pour net/layer.
    t0 = time.perf_counter()
    corridors, min_thick = pour_corridors(model, grid)
    corridors_s = time.perf_counter() - t0

    # 3. severance_cost build for a representative signal width.
    t0 = time.perf_counter()
    sev = severance_cost(corridors, min_thick, grid,
                         width_mm=params.track_width_mm,
                         clearance_mm=params.clearance_mm,
                         weight=params.pour_severance_weight)
    severance_s = time.perf_counter() - t0

    # 4. island facts (cheap: reuses labeled path via gnd_islands).
    t0 = time.perf_counter()
    islands = gnd_islands(model, grid)
    islands_s = time.perf_counter() - t0
    per_layer = {layer: len(v) for layer, v in islands.items()}
    unfed = sum(1 for v in islands.values() for isl in v
                if not isl.has_connection)
    return {
        "pour_nets": nets,
        "label_s": label_s,
        "corridors_s": corridors_s,
        "severance_s": severance_s,
        "islands_s": islands_s,
        "islands_per_layer": per_layer,
        "islands_total": sum(per_layer.values()),
        "unfed_islands": unfed,
        "severance_nonzero": int((sev != 0).sum()),
    }


def time_stitch(model, grid, max_vias: int, max_bridges: int,
                max_bridge_mm: float) -> dict:
    t0 = time.perf_counter()
    plan = plan_stitches(model, grid, max_vias=max_vias,
                         max_bridges=max_bridges,
                         max_bridge_mm=max_bridge_mm)
    elapsed = time.perf_counter() - t0
    # Determinism probe: rerun must match.
    plan2 = plan_stitches(model, grid, max_vias=max_vias,
                          max_bridges=max_bridges,
                          max_bridge_mm=max_bridge_mm)
    deterministic = (
        len(plan.vias) == len(plan2.vias)
        and len(plan.tracks) == len(plan2.tracks)
        and plan.islands_before == plan2.islands_before
        and plan.islands_after == plan2.islands_after
    )
    return {
        "max_vias": max_vias,
        "max_bridges": max_bridges,
        "islands_before": plan.islands_before,
        "islands_after": plan.islands_after,
        "repaired": plan.islands_before - plan.islands_after,
        "vias": len(plan.vias),
        "tracks": len(plan.tracks),
        "stitch_s": round(elapsed, 3),
        "deterministic": deterministic,
    }


def time_case(label: str, model, grid, params: RoutingParams,
              budgets: list[tuple[int, int]]) -> dict:
    cells_m = len(grid.layers) * grid.width_cells * grid.height_cells / 1e6
    pour = time_pour_stages(model, grid, params)
    stitches = [time_stitch(model, grid, mv, mb, params.stitch_bridge_max_mm)
                for mv, mb in budgets]
    stitch_total = sum(s["stitch_s"] for s in stitches)
    pour_total = (pour["label_s"] + pour["corridors_s"]
                  + pour["severance_s"] + pour["islands_s"])
    return {
        "case": label,
        "grid_cells_M": round(cells_m, 2),
        "grid_WxH": [grid.width_cells, grid.height_cells],
        "pour": {k: (round(v, 3) if isinstance(v, float) else v)
                 for k, v in pour.items()},
        "pour_total_s": round(pour_total, 3),
        "stitches": stitches,
        "ms_per_Mcell_pour": round(pour_total / max(cells_m, 1e-9) * 1000, 1),
        "ms_per_Mcell_stitch": round(
            stitches[0]["stitch_s"] / max(cells_m, 1e-9) * 1000, 1
        ) if stitches else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true",
                    help="CI smoke: synthetics + tube111, single budget")
    ap.add_argument("--out", default=None, help="results JSON path")
    ap.add_argument("--resolution", type=float, default=0.1,
                    help="grid resolution in mm")
    ap.add_argument("--resolutions", type=float, nargs="+", default=None,
                    help="resolution probe list (default [0.1, 0.2])")
    args = ap.parse_args()

    params = RoutingParams(grid_resolution_mm=args.resolution)
    full_budgets = [(32, 16), (8, 4), (0, 0)]
    quick_budgets = [(32, 16)]

    if args.quick:
        cases = [
            ("synth-overlap", synthetic_overlap()),
            ("synth-bridge", synthetic_bridge()),
            ("tube111", load_board("tube111")),
        ]
        budgets = quick_budgets
        resolutions: dict[str, list[float]] = {c[0]: [args.resolution]
                                               for c in cases}
    else:
        cases = [
            ("synth-overlap", synthetic_overlap()),
            ("synth-bridge", synthetic_bridge()),
            ("minimal", load_board("minimal")),
            ("ghost_test", load_ghost_test()),
            ("tube111", load_board("tube111")),
            ("DCCF", load_board("DCCF")),
        ]
        budgets = full_budgets
        probe = args.resolutions or [0.1, 0.2]
        # Resolution probe on tube111 and DCCF (DCCF at 0.2mm now fast enough).
        resolutions = {label: ([args.resolution] if label not in ("tube111", "DCCF")
                               else sorted(set([args.resolution] + probe)))
                       for label, _ in cases}

    rows = []
    for label, model in cases:
        for res in resolutions[label]:
            grid = create_grid_from_model(model, res, params.margin_mm)
            p = RoutingParams(grid_resolution_mm=res)
            rows.append(time_case(
                f"{label}@{res}mm", model, grid, p, budgets))

    print(f"{'case':<22}{'cellsM':>8}{'isl':>6}{'pour_s':>9}"
          f"{'stitch_s':>10}{'before→after':>13}  vias/trks")
    for r in rows:
        st0 = r["stitches"][0]
        print(f"{r['case']:<22}{r['grid_cells_M']:>8.2f}"
              f"{r['pour']['islands_total']:>6}"
              f"{r['pour_total_s']:>9.3f}{st0['stitch_s']:>10.3f}"
              f"{st0['islands_before']:>5}→{st0['islands_after']:<5}  "
              f"{st0['vias']}/{st0['tracks']}"
              f"{'' if st0['deterministic'] else '  NONDET!'}")
        for st in r["stitches"][1:]:
            print(f"  budget ({st['max_vias']:>2},{st['max_bridges']:>2}): "
                  f"{st['islands_before']}→{st['islands_after']} "
                  f"vias={st['vias']} trks={st['tracks']} "
                  f"{st['stitch_s']:.3f}s")

    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks"
        / f"stitch_pour_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"params": params.to_dict(), "budgets": budgets, "cases": rows},
        indent=2) + "\n")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
