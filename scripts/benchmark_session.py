#!/usr/bin/env python3
"""App-session benchmark: placement rounds + 50-net routing + shuffle re-routes.

Mimics a real working session in the UI on one board (default tube111;
pass --board for a fixture name or a .kicad_pcb path, e.g. a tube116 file):

  Phase A — placement rounds on parts sets: movable footprints are split
      into ``--groups`` sets (sorted by ref, like hand-picked UI
      selections); each set runs ``run_placement`` with the cluster
      preset and commits via ``commit_placement`` (same rip-affected-
      copper semantics as Accept). Repeated for ``--passes`` passes.
  Phase B — legalize rounds: full-board legalize preset
      (app-params.js REGIME_PRESETS.legalize) with a higher iteration
      budget (``--legalize-iters``), committed between rounds.
  Phase C — tidy rounds: tidy/align preset (weak angle snap + row hold),
      short budget, committed between rounds.
  Phase D — track routing: the first ``--nets`` signal nets routed in
      batches of ``--batch`` through ``route_nets`` with a persistent
      (cost_grid, routing_grid, ObstacleCache) triple — the incremental
      production path. Copper is stamped back into the working model via
      ``routes_to_tracks_vias`` so later batches route against earlier
      copper, exactly like sequential app jobs.
  Phase E — shuffle re-routing: ``--shuffles`` seeded random single-net
      rip + re-route rolls (the Shuffle button), each timed separately.
      Rolls record per-stage timings plus edge-case signals (failure
      reasons, emit-gate fallbacks) and dedupe into a signature report;
      run totals are compared against the committed tube111 baseline.

Legality (``check_legality``) is reported after each placement phase.
Stitching and DRC stay off so the numbers reflect placement + routing;
re-run with --with-stitch for one stitching probe on top.

Usage (under the project .venv)::

    .venv/bin/python scripts/benchmark_session.py [--board tube111]
        [--nets 50 --batch 10 --shuffles 10] [--quick] [--out PATH]

--quick is a <60s smoke (2 groups, few nets/rolls). Full default is a
long run (~10-20 min on tube111). Results print as tables and are saved
as JSON (default docs/benchmarks/session_<board>_<date>.json).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import (  # noqa: E402
    ArcTrack,
    Point,
    Track as BoardTrack,
    Via as BoardVia,
    board_model,
    is_ground_net,
)
from kicad_autorouter.board_model.query import commit_placement  # noqa: E402
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402
from kicad_autorouter.placement.legality import check_legality  # noqa: E402
from kicad_autorouter.routing.obstacles import ObstacleCache  # noqa: E402
from kicad_autorouter.routing.output import routes_to_tracks_vias  # noqa: E402
from kicad_autorouter.routing.pipeline import (  # noqa: E402
    RoutingParams,
    route_nets,
)
from kicad_autorouter.routing.route_raw import (  # noqa: E402
    _rip_up_nets_from_model,
)
from kicad_autorouter.sexpr import parse_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

POWER_NAMES = {"VIN", "+12V", "-12V", "VDD", "VCC", "VEE", "+5V", "-5V", "+3V3"}

# Regime presets mirrored from ui/app-params.js REGIME_PRESETS (calibrated
# starting points; dt/vmax/friction ride along via merge-POST there).
CLUSTER = dict(attraction_k_N=0.05, wall_k_N=0.025, damping_Ns_m=0.08,
               dt_s=2e-3, friction_N=1e-6, wall_halo_m=0.006, wall_exponent=1.0)
LEGALIZE = dict(attraction_k_N=0.008, wall_k_N=0.1, damping_Ns_m=0.12,
                dt_s=1e-3, friction_N=1e-6, wall_halo_m=0.004,
                wall_exponent=2.0)
TIDY = dict(attraction_k_N=0.002, wall_k_N=0.1, damping_Ns_m=0.12,
            dt_s=1e-3, friction_N=1e-5, wall_halo_m=0.004, wall_exponent=2.0,
            align_angle_k_N=3e-3, align_pos_k_N_m=0.5)


def load_board(spec: str):
    """Fixture name (tube111, DCCF, minimal) or a .kicad_pcb path."""
    p = Path(spec)
    if p.exists():
        return board_model(parse_file(p))
    cand = FIXTURES / f"{spec}.kicad_pcb"
    if not cand.exists():
        cand = FIXTURES / f"{spec}.sved.kicad_pcb"
    return board_model(parse_file(cand))


def pick_session_nets(model, k: int) -> list[str]:
    """First ``k`` signal nets, smallest pad-count first.

    Bands widen (2-4 pads, then 2-8) so a 50-net session is reachable on
    mid-size boards; power/ground nets are skipped (app routes them
    separately via the power flow).
    """
    cands: list[tuple[int, str]] = []

    def _band(lo: int, hi: int) -> None:
        for name, conns in model.nets.items():
            if not lo <= len(conns) <= hi:
                continue
            if name.upper() in POWER_NAMES or is_ground_net(name):
                continue
            if name in {n for _, n in cands}:
                continue
            cands.append((len(conns), name))

    _band(2, 4)
    _band(5, 8)
    cands.sort()
    return [name for _, name in cands[:k]]


def footprint_groups(model, n_groups: int) -> list[set[str]]:
    """Split movable footprints into ``n_groups`` ref-ordered sets."""
    refs = sorted(fp.uuid for fp in model.footprints
                  if fp.uuid and not fp.locked)
    return [set(refs[i::n_groups]) for i in range(n_groups)]


def legality_summary(model) -> dict:
    rep = check_legality(model, {})
    return {"legal": bool(rep.is_legal),
            "max_overlap_mm2": round(rep.max_pair_overlap_mm2, 3)}


def run_phase_a(model, params: PlacementParams, groups: list[set[str]],
                passes: int) -> tuple:
    """Placement rounds, one group at a time, committing between rounds."""
    rows = []
    for p in range(passes):
        for gi, g in enumerate(groups):
            t0 = time.perf_counter()
            proposal = run_placement(model, params, movable_uuids=g)
            wall = time.perf_counter() - t0
            model = commit_placement(model, proposal.deltas)
            rows.append({
                "pass": p, "group": gi, "parts": len(g),
                "iters": proposal.iterations,
                "stop": getattr(proposal, "stop_reason", "?"),
                "moved": len(proposal.deltas), "wall_s": round(wall, 3),
                "elapsed_s": round(getattr(proposal, "elapsed_s", wall), 3),
            })
            print(f"  A p{p} g{gi}: {len(g):>4} parts "
                  f"{proposal.iterations:>4}it "
                  f"{getattr(proposal, 'stop_reason', '?'):>9} "
                  f"moved={len(proposal.deltas):>4} {wall:7.2f}s",
                  flush=True)
    return model, rows


def run_regime_rounds(model, label: str, preset: dict, iters: int,
                      rounds: int) -> tuple:
    """Full-board regime rounds (legalize/tidy), committing between rounds."""
    rows = []
    for r in range(rounds):
        params = PlacementParams(**{**preset, "max_iterations": iters})
        t0 = time.perf_counter()
        proposal = run_placement(model, params)
        wall = time.perf_counter() - t0
        model = commit_placement(model, proposal.deltas)
        rows.append({
            "round": r, "iters": proposal.iterations,
            "stop": getattr(proposal, "stop_reason", "?"),
            "moved": len(proposal.deltas), "wall_s": round(wall, 3),
            "final_disp_mm": round(
                getattr(proposal, "final_max_disp_mm", 0.0), 4),
        })
        print(f"  {label} r{r}: {proposal.iterations:>4}it "
              f"{getattr(proposal, 'stop_reason', '?'):>9} "
              f"moved={len(proposal.deltas):>4} {wall:7.2f}s", flush=True)
    return model, rows


def stamp_copper(model, result, grid, params: RoutingParams):
    """Fold freshly routed copper back into the working model.

    ``routes_to_tracks_vias`` emits routing-output (mutable) copper; the
    model (and the obstacle cache's copper set) needs the frozen
    board_model types, so convert here — same copper production writes
    to the tree.
    """
    routes = dict(getattr(result, "net_routes", None) or {})
    if not routes:
        return model
    tracks, vias, arcs = routes_to_tracks_vias(
        routes, grid,
        net_class_widths=dict(getattr(result, "width_map", None) or {}),
        default_width_mm=params.track_width_mm,
        power_width_mm=params.power_width_mm,
        via_size_mm=params.via_size_mm, via_drill_mm=params.via_drill_mm,
    )
    return replace(model,
                   tracks=model.tracks + tuple(
                       BoardTrack(start=Point(*t.start), end=Point(*t.end),
                                  width_mm=t.width_mm, layer=t.layer,
                                  net_name=t.net_name) for t in tracks),
                   vias=model.vias + tuple(
                       BoardVia(position=Point(*v.position), size_mm=v.size_mm,
                                drill_mm=v.drill_mm, layers=tuple(v.layers),
                                net_name=v.net_name) for v in vias),
                   arcs=model.arcs + tuple(
                       ArcTrack(start=Point(*a.start), mid=Point(*a.mid),
                                end=Point(*a.end), width_mm=a.width_mm,
                                layer=a.layer, net_name=a.net_name)
                       for a in arcs))


def run_phase_routing(model, nets: list[str], batch: int,
                      params: RoutingParams) -> tuple:
    """Route ``nets`` in batches with persistent incremental state."""
    cache = ObstacleCache()
    cost_grid, grid = None, None
    rows, routed_so_far = [], []
    for i in range(0, len(nets), batch):
        chunk = nets[i:i + batch]
        t0 = time.perf_counter()
        result, cost_grid, grid = route_nets(
            model, chunk, set(), cost_grid=cost_grid, routing_grid=grid,
            obstacle_cache=cache, params=params)
        wall = time.perf_counter() - t0
        model = stamp_copper(model, result, grid, params)
        routed_so_far += chunk
        t = dict(result.timings)
        rows.append({
            "batch": i // batch, "nets": chunk,
            "routed": result.nets_routed, "failed": result.nets_failed,
            "expansions": result.expansions,
            "wall_s": round(wall, 3),
            "astar_s": round(t.get("astar_s", 0.0), 3),
            "smooth_s": round(t.get("smooth_s", 0.0), 3),
            "smooth_pull_s": round(t.get("smooth_pull_s", 0.0), 3),
            "smooth_fit_s": round(t.get("smooth_fit_s", 0.0), 3),
            "smooth_verify_s": round(t.get("smooth_verify_s", 0.0), 3),
            "smooth_verify_build_s": round(t.get("smooth_verify_build_s", 0.0), 3),
            "smooth_verify_segs": int(t.get("smooth_verify_segs", 0) or 0),
            "pour_s": round(t.get("pour_s", 0.0), 3),
            "occupancy_s": round(t.get("occupancy_s", 0.0), 3),
        })
        print(f"  D batch{i // batch}: {len(chunk):>3} nets "
              f"ok={result.nets_routed} fail={result.nets_failed} "
              f"exp={result.expansions:<8} wall={wall:7.2f}s "
              f"(astar {t.get('astar_s', 0):.2f} smooth "
              f"{t.get('smooth_s', 0):.2f} "
              f"[pull {t.get('smooth_pull_s', 0):.2f} fit "
              f"{t.get('smooth_fit_s', 0):.2f} ver "
              f"{t.get('smooth_verify_s', 0):.2f} "
              f"(build {t.get('smooth_verify_build_s', 0):.2f}, "
              f"{int(t.get('smooth_verify_segs', 0) or 0)} segs)] pour "
              f"{t.get('pour_s', 0):.2f})", flush=True)
    return model, rows, cache, cost_grid, grid


def run_phase_shuffle(model, pool: list[str], n_rolls: int, seed: int,
                      params: RoutingParams, cache, cost_grid,
                      grid) -> tuple[list[dict], dict]:
    """Seeded random single-net rip + re-route rolls (Shuffle button).

    Each roll records its full stage timings plus edge-case signals
    (failure reasons, emit-gate fallback count, copper added) so rolls
    double as a fuzz log: any new signature replays deterministically
    from the returned seed + queue.
    """
    rng = random.Random(seed)
    queue = [rng.choice(pool) for _ in range(n_rolls)]
    rows = []
    for i, net in enumerate(queue):
        # The app rip endpoint drops the persistent grids (full rebuild on
        # the next route) but keeps the obstacle cache, which re-syncs
        # add/remove from the model — mirror that here.
        ripped = _rip_up_nets_from_model(model, [net])
        t0 = time.perf_counter()
        result, cost_grid, grid = route_nets(
            ripped, [net], set(), cost_grid=None, routing_grid=None,
            obstacle_cache=cache, params=params)
        wall = time.perf_counter() - t0
        ok = result.nets_routed == 1 and not result.nets_failed
        if ok:
            model = stamp_copper(ripped, result, grid, params)
        else:
            model = ripped
        t = dict(result.timings)
        rows.append({"roll": i, "net": net, "ok": ok,
                     "expansions": result.expansions,
                     "wall_s": round(wall, 3),
                     "astar_s": round(t.get("astar_s", 0.0), 3),
                     "smooth_s": round(t.get("smooth_s", 0.0), 3),
                     "smooth_pull_s": round(t.get("smooth_pull_s", 0.0), 3),
                     "smooth_fit_s": round(t.get("smooth_fit_s", 0.0), 3),
                     "smooth_verify_s": round(t.get("smooth_verify_s", 0.0), 3),
                     "smooth_verify_build_s": round(t.get("smooth_verify_build_s", 0.0), 3),
                     "smooth_verify_segs": int(t.get("smooth_verify_segs", 0) or 0),
                     "pour_s": round(t.get("pour_s", 0.0), 3),
                     "occupancy_s": round(t.get("occupancy_s", 0.0), 3),
                     "grid_build_s": round(t.get("grid_build_s", 0.0), 3),
                     "failed_reasons": dict(result.failed_reasons or {}),
                     "gate_fallbacks": int(t.get("emit_gate_fallback_raw", 0) or 0),
                     "tracks_added": result.tracks_added,
                     "vias_added": result.vias_added,
                     "arcs_added": result.arcs_added,
                     "wire_mm": round(result.total_wire_mm, 2)})
        flag = "ok " if ok else f"FAIL {list((result.failed_reasons or {}).values())[:1]}"
        print(f"  E roll{i}: {net:<28} {flag} "
              f"exp={result.expansions:<7} {wall:7.2f}s "
              f"(a {t.get('astar_s', 0):.2f} s {t.get('smooth_s', 0):.2f} "
              f"p {t.get('pour_s', 0):.2f})", flush=True)
    return rows, {"seed": seed, "queue": queue}


def shuffle_signatures(rows: list[dict]) -> list[dict]:
    """Dedupe shuffle rolls into edge-case signatures.

    Signature = (ok?, failure reason text, gate-fallback tripped?).
    Report-only: surfaces new behaviors without asserting on them.
    """
    groups: dict[tuple, list[int]] = {}
    for r in rows:
        reasons = r.get("failed_reasons") or {}
        reason = "; ".join(f"{n}: {v}" for n, v in sorted(reasons.items()))
        key = (bool(r["ok"]), reason, int(r.get("gate_fallbacks", 0)) > 0)
        groups.setdefault(key, []).append(r["roll"])
    out = []
    for (ok, reason, gated), rolls in sorted(groups.items(), key=lambda kv: kv[1]):
        nets = [rows[i]["net"] for i in rolls]
        out.append({"status": "ok" if ok else "FAIL",
                    "reason": reason, "gate_fallback": gated,
                    "rolls": rolls, "nets": nets})
    return out


def baseline_drift(board: str, rows_d: list[dict],
                   rows_e: list[dict]) -> list[str]:
    """Compare run totals vs the committed tube111 session baseline.

    Read-only guardrail: reports drift in failures, emit-gate fallbacks
    and wire length without asserting (small changes are permissible).
    Baselines lacking newer fields report them as n/a.
    """
    lines: list[str] = []
    base_path = (REPO_ROOT / "docs" / "benchmarks"
                 / "session_tube111_2026-11-09.json")
    if board != "tube111" or not base_path.exists():
        return ["(no tube111 baseline to compare — skipping drift check)"]
    try:
        base = json.loads(base_path.read_text())
    except (OSError, ValueError) as exc:
        return [f"(baseline unreadable: {exc})"]

    def _totals(routing, shuffle):
        fails = (sum(r.get("failed", 0) for r in routing)
                 + sum(0 if r.get("ok") else 1 for r in shuffle))
        wire = round(sum(r.get("wire_mm", 0.0) for r in shuffle), 2)
        gates = sum(int(r.get("gate_fallbacks", 0) or 0) for r in shuffle)
        scope = (f"{sum(len(r.get('nets', [])) for r in routing)} nets, "
                 f"{len(shuffle)} rolls")
        return fails, wire, gates, scope

    b_fails, b_wire, b_gates, b_scope = _totals(base.get("routing", []),
                                               base.get("shuffle", []))
    c_fails, c_wire, c_gates, c_scope = _totals(rows_d, rows_e)
    b_has_gates = any("gate_fallbacks" in r for r in base.get("shuffle", []))
    lines.append(f"fails: baseline {b_fails} ({b_scope}) "
                 f"vs current {c_fails} ({c_scope})")
    lines.append(f"shuffle wire_mm total: baseline {b_wire} vs current {c_wire} "
                 f"(comparable only at equal rolls/nets)")
    lines.append("shuffle gate fallbacks: "
                 + (f"baseline {b_gates} vs current {c_gates}"
                    if b_has_gates else f"baseline n/a vs current {c_gates}"))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--board", default="tube111",
                    help="fixture name or .kicad_pcb path")
    ap.add_argument("--nets", type=int, default=50,
                    help="signal nets to route in phase D")
    ap.add_argument("--batch", type=int, default=10,
                    help="nets per route_nets call in phase D")
    ap.add_argument("--groups", type=int, default=4,
                    help="parts sets in phase A")
    ap.add_argument("--passes", type=int, default=2,
                    help="passes over the parts sets")
    ap.add_argument("--cluster-iters", type=int, default=150)
    ap.add_argument("--legalize-iters", type=int, default=600,
                    help="higher budget per legalize round")
    ap.add_argument("--legalize-rounds", type=int, default=2)
    ap.add_argument("--tidy-rounds", type=int, default=2)
    ap.add_argument("--tidy-iters", type=int, default=150)
    ap.add_argument("--shuffles", type=int, default=10,
                    help="shuffle re-route rolls in phase E")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quick", action="store_true",
                    help="smoke: tiny budgets, few nets/rolls")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.quick:
        args.groups, args.passes = 2, 1
        args.cluster_iters = 10
        args.legalize_iters, args.legalize_rounds = 10, 1
        args.tidy_iters, args.tidy_rounds = 10, 1
        args.nets, args.batch, args.shuffles = 6, 3, 2

    model = load_board(args.board)
    board = Path(args.board).stem if Path(args.board).exists() else args.board
    print(f"board={board} footprints={len(model.footprints)} "
          f"nets={len(model.nets)}", flush=True)

    rparams = RoutingParams(max_attempts_per_net=1, stitch_gnd_vias=False,
                            run_drc=False)

    t_all = time.perf_counter()
    print(f"Phase A: {args.passes}x{args.groups} placement rounds "
          f"({args.cluster_iters}it, cluster preset)", flush=True)
    groups = footprint_groups(model, args.groups)
    model, rows_a = run_phase_a(
        model, PlacementParams(**{**CLUSTER,
                                  "max_iterations": args.cluster_iters}),
        groups, args.passes)
    legal_a = legality_summary(model)
    print(f"  legality after A: {legal_a}", flush=True)

    print(f"Phase B: {args.legalize_rounds} legalize rounds "
          f"({args.legalize_iters}it)", flush=True)
    model, rows_b = run_regime_rounds(model, "B", LEGALIZE,
                                      args.legalize_iters,
                                      args.legalize_rounds)
    legal_b = legality_summary(model)
    print(f"  legality after B: {legal_b}", flush=True)

    print(f"Phase C: {args.tidy_rounds} tidy rounds ({args.tidy_iters}it)",
          flush=True)
    model, rows_c = run_regime_rounds(model, "C", TIDY, args.tidy_iters,
                                      args.tidy_rounds)
    legal_c = legality_summary(model)
    print(f"  legality after C: {legal_c}", flush=True)

    nets = pick_session_nets(model, args.nets)
    print(f"Phase D: routing {len(nets)} nets in batches of {args.batch}",
          flush=True)
    model, rows_d, cache, cost_grid, grid = run_phase_routing(
        model, nets, args.batch, rparams)

    print(f"Phase E: {args.shuffles} shuffle rolls (seed {args.seed})",
          flush=True)
    rows_e, shuffle_meta = run_phase_shuffle(model, nets, args.shuffles,
                                             args.seed, rparams, cache,
                                             cost_grid, grid)
    sigs = shuffle_signatures(rows_e)
    print("  shuffle signatures:", flush=True)
    for s in sigs:
        print(f"    {s['status']:<4} x{len(s['rolls']):>2} rolls={s['rolls']} "
              f"gate_fallback={s['gate_fallback']} "
              f"{s['reason'] or '(clean)'}", flush=True)
    print("  drift vs baseline:", flush=True)
    for line in baseline_drift(board, rows_d, rows_e):
        print(f"    {line}", flush=True)

    total = time.perf_counter() - t_all
    ok_e = sum(1 for r in rows_e if r["ok"])
    exp_d = sum(r["expansions"] for r in rows_d)
    print(f"\nDone in {total:.1f}s: A={sum(r['wall_s'] for r in rows_a):.1f}s "
          f"B={sum(r['wall_s'] for r in rows_b):.1f}s "
          f"C={sum(r['wall_s'] for r in rows_c):.1f}s "
          f"D={sum(r['wall_s'] for r in rows_d):.1f}s "
          f"({exp_d} expansions) "
          f"E={sum(r['wall_s'] for r in rows_e):.1f}s ({ok_e}/{len(rows_e)} ok)",
          flush=True)

    out = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "benchmarks"
        / f"session_{board}_{date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "board": board, "total_s": round(total, 3),
        "placement": rows_a, "legalize": rows_b, "tidy": rows_c,
        "routing": rows_d, "shuffle": rows_e,
        "shuffle_meta": shuffle_meta, "shuffle_signatures": sigs,
        "legality": {"after_a": legal_a, "after_b": legal_b,
                     "after_c": legal_c},
        "params": {"routing": rparams.to_dict(), "cluster_iters": args.cluster_iters,
                   "legalize_iters": args.legalize_iters, "tidy_iters": args.tidy_iters,
                   "seed": args.seed, "shuffle_rolls": args.shuffles},
    }, indent=2) + "\n")
    print(f"Saved {out}", flush=True)


if __name__ == "__main__":
    main()
