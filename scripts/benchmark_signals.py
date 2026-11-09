#!/usr/bin/env python3
"""Deep signal-net routing benchmark: full signal set, system defaults.

Mirrors the ``place_strip.py`` signals stage exactly (same board-mode
overlay from ``strategy.apply_mode`` / ``infer_mode``, same rails
protection, same ``strip_gnd_blockers=false`` on existing copper) but
routes ALL signal nets in one ``route_nets`` call and records per-net
cost attribution from ``RoutingResult.net_stats`` (added for this: wall
seconds, expansions, and numeric timing deltas per net).

Usage:
    .venv/bin/python scripts/benchmark_signals.py [--quick]
    .venv/bin/python scripts/benchmark_signals.py --board tmp/tube115_rails.kicad_pcb

``--quick`` routes the first 3 signals as a smoke test. Full runs take
several minutes; JSON goes to ``docs/benchmarks/`` by default.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import board_model  # noqa: E402
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets  # noqa: E402
from kicad_autorouter.routing.power import identify_power_nets  # noqa: E402
from kicad_autorouter.sexpr import parse_file, to_sexpr  # noqa: E402
from kicad_autorouter.strategy import apply_mode, infer_mode  # noqa: E402


def net_pad_counts(model):
    counts = Counter()
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name:
                counts[pad.net_name] += 1
    return counts


def copper_by_net(model):
    """Per-net emitted copper geometry: wire, counts, layer split."""
    stats = {}

    def entry(net):
        return stats.setdefault(net, {
            "wire_mm": 0.0, "tracks": 0, "vias": 0, "arcs": 0,
            "layers": Counter(),
        })

    for t in list(model.tracks):
        if not t.net_name:
            continue
        e = entry(t.net_name)
        e["wire_mm"] += math.hypot(
            t.end.x_mm - t.start.x_mm, t.end.y_mm - t.start.y_mm)
        e["tracks"] += 1
        e["layers"][t.layer] += 1
    for a in list(model.arcs):
        if not a.net_name:
            continue
        e = entry(a.net_name)
        e["wire_mm"] += (
            math.hypot(a.mid.x_mm - a.start.x_mm, a.mid.y_mm - a.start.y_mm)
            + math.hypot(a.end.x_mm - a.mid.x_mm, a.end.y_mm - a.mid.y_mm)
        )
        e["arcs"] += 1
        e["layers"][a.layer] += 1
    for v in list(model.vias):
        if not v.net_name:
            continue
        e = entry(v.net_name)
        e["vias"] += 1
    for e in stats.values():
        e["layers"] = dict(e["layers"])
    return stats


def main(argv=None) -> int:
    repo = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description="Full-signal routing benchmark.")
    p.add_argument("--board",
                   default=str(repo / "tmp" / "tube115_rails.kicad_pcb"))
    p.add_argument("--out", default=None)
    p.add_argument("--save-pcb", default=None,
                   help="write the routed tree here (default: no save)")
    p.add_argument("--quick", action="store_true",
                   help="route the first 3 signals only (smoke test)")
    args = p.parse_args(argv)

    tree = parse_file(Path(args.board))
    model = board_model(tree)
    mode = infer_mode(model.copper_layers)
    route_params = RoutingParams.from_dict(apply_mode({}, mode))
    if route_params.strip_gnd_blockers:
        # Same as the place_strip signals stage: never evict committed
        # rails copper from under the signal routes.
        route_params = replace(route_params, strip_gnd_blockers=False)
    counts = net_pad_counts(model)
    power, ground, _ = identify_power_nets(model, ())
    rails = [n for n in list(ground) + list(power) if counts.get(n, 0) >= 2]
    routable = [n for n in model.nets if counts.get(n, 0) >= 2]
    signals = [n for n in routable if n not in set(rails)]
    if args.quick:
        signals = signals[:3]
    print(f"board={args.board} mode={mode} grid={route_params.grid_resolution_mm}mm "
          f"track={route_params.track_width_mm}/power={route_params.power_width_mm}mm "
          f"rails={len(rails)} signals={len(signals)}")

    t0 = time.perf_counter()
    result, _, _ = route_nets(
        model=model,
        net_names=list(signals),
        protected_nets=set(rails),
        params=route_params,
        original_pcb_tree=tree,
    )
    wall = time.perf_counter() - t0
    print(f"routed={result.nets_routed} failed={result.nets_failed} "
          f"tracks+={result.tracks_added} vias+={result.vias_added} "
          f"arcs+={result.arcs_added} expansions={result.expansions} "
          f"wire={result.total_wire_mm:.0f}mm time={wall:.1f}s")
    for net, reason in (result.failed_reasons or {}).items():
        print(f"FAILED {net}: {reason}")

    # Per-net table: wall seconds, expansions, smooth/astar/pour split.
    rows = []
    for net in signals:
        st = (result.net_stats or {}).get(net, {})
        tm = st.get("timings", {})
        rows.append({
            "net": net,
            "ok": st.get("ok", False),
            "pads": counts.get(net, 0),
            "wall_s": round(st.get("wall_s", 0.0), 3),
            "expansions": st.get("expansions", 0),
            "expansions_failed": st.get("expansions_failed", 0),
            "astar_s": round(tm.get("astar_s", 0.0), 3),
            "smooth_s": round(tm.get("smooth_s", 0.0), 3),
            "smooth_verify_build_s": round(tm.get("smooth_verify_build_s", 0.0), 3),
            "pour_s": round(tm.get("pour_s", 0.0), 3),
            "fail": (result.failed_reasons or {}).get(net),
        })
    rows.sort(key=lambda r: -r["wall_s"])
    print(f"{'net':22s} {'pads':>4s} {'wall':>7s} {'exp':>8s} {'fail_exp':>8s} {'astar':>6s} {'smooth':>6s} {'build':>6s} {'pour':>6s}")
    for r in rows[:20]:
        print(f"{r['net']:22s} {r['pads']:4d} {r['wall_s']:7.2f} {r['expansions']:8d} "
              f"{r['expansions_failed']:8d} {r['astar_s']:6.2f} {r['smooth_s']:6.2f} "
              f"{r['smooth_verify_build_s']:6.2f} {r['pour_s']:6.2f}")

    new_tree = result.pcb_tree if result.pcb_tree is not None else tree
    copper = copper_by_net(board_model(new_tree))
    for r in rows:
        c = copper.get(r["net"], {})
        r["wire_mm"] = round(c.get("wire_mm", 0.0), 2)
        r["tracks"] = c.get("tracks", 0)
        r["vias"] = c.get("vias", 0)
        r["arcs"] = c.get("arcs", 0)

    payload = {
        "params": {
            "board": args.board,
            "mode": mode,
            "quick": args.quick,
            "rails": rails,
            "signals": signals,
            "route_params": route_params.to_dict(),
        },
        "totals": {
            "nets_routed": result.nets_routed,
            "nets_failed": result.nets_failed,
            "tracks_added": result.tracks_added,
            "vias_added": result.vias_added,
            "arcs_added": result.arcs_added,
            "expansions": result.expansions,
            "total_wire_mm": round(result.total_wire_mm, 2),
            "wall_s": round(wall, 2),
            "timings": {k: (round(v, 3) if isinstance(v, float) else v)
                        for k, v in (result.timings or {}).items()},
            "failed_reasons": dict(result.failed_reasons or {}),
        },
        "nets": rows,
    }
    out = Path(args.out) if args.out else (
        repo / "docs" / "benchmarks"
        / f"signals_{Path(args.board).stem}_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"saved -> {out}")
    if args.save_pcb:
        Path(args.save_pcb).write_text(to_sexpr(new_tree) + "\n", encoding="utf-8")
        print(f"saved pcb -> {args.save_pcb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
