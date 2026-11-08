#!/usr/bin/env python3
"""Staged place -> strip -> legalize pipeline with intermediate board state.

Builds from a test fixture (default: tube111 worktree) through the same
accept semantics the app uses (``BoardState.accept_proposal``), so each
placement step commits footprints AND rips affected-net copper before the
next step simulates:

    filed -> [cluster steps + rip] -> tmp/tube112.kicad_pcb (stripped)
         -> [legalize steps + rip] -> tmp/tube112_legal.kicad_pcb

Regime presets are copied from ``ui/app-params.js`` (Cluster scatters,
Legalize settles placed boards; see ``docs/si_force_laws.md`` section 3).

DRC triage ignores ``unconnected_items`` by design (a stripped board is
supposed to be full of them) and diffs violation types against the input
board baseline, so only placement-geometry classes show up.

Runs under the project .venv (pure Python; only DRC needs `kicad-cli`):

    .venv/bin/python scripts/place_strip.py [--cluster-iters 150]
        [--legal-iters 500] [--no-drc]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import (  # noqa: E402
    apply_deltas,
    board_model,
    commit_placement,
    seed_power_nets_from_netclasses,
)
from kicad_autorouter.io import (  # noqa: E402
    nudge_footprint_by_uuid,
    rip_up_nets,
)
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402
from kicad_autorouter.routing.power import identify_power_nets  # noqa: E402
from kicad_autorouter.sexpr import parse_file, to_sexpr  # noqa: E402
from kicad_autorouter.validate import KicadCliNotFound, validate_pcb  # noqa: E402

# Regime presets from ui/app-params.js (applyRegimePreset cluster/legalize).
CLUSTER = {
    "attraction_k_N": 0.05,
    "wall_k_N": 0.025,
    "damping_Ns_m": 0.08,
    "dt_s": 2e-3,
    "vmax_m_s": 1.0,
    "friction_N": 1e-6,
    "wall_halo_m": 0.006,
    "cross_layer_pin_halo_m": 0.0007,
    "wall_exponent": 1.0,
    "convergence_eps_m": 2e-5,
    "align_angle_k_N": 0,
    "align_pos_k_N_m": 0,
    "align_deadband_deg": 10,
    "align_radius_m": 0.020,
}
LEGALIZE = {
    "attraction_k_N": 0.008,
    "wall_k_N": 0.1,
    "damping_Ns_m": 0.12,
    "dt_s": 1e-3,
    "vmax_m_s": 1.0,
    "friction_N": 1e-6,
    "wall_halo_m": 0.004,
    "cross_layer_pin_halo_m": 0.0007,
    "wall_exponent": 2.0,
    "convergence_eps_m": 2e-5,
    "align_angle_k_N": 0,
    "align_pos_k_N_m": 0,
    "align_deadband_deg": 10,
    "align_radius_m": 0.020,
}


def seed_protected(model):
    """Replicate the server's protected-net seeding on board load."""
    overrides = set(seed_power_nets_from_netclasses(model))
    power_nets, ground_nets, _ = identify_power_nets(model, overrides)
    return (
        set(power_nets)
        | set(ground_nets)
        | {"GND", "GND_PWR", "VCC", "VDD", "VSS", "GROUND"}
    )


def affected_nets(model, moved_uuids):
    """Union of pad nets over moved footprints (mirrors accept_proposal)."""
    nets = set()
    for fp in model.footprints:
        if fp.uuid in moved_uuids:
            for pad in fp.pads:
                if pad.net_name:
                    nets.add(pad.net_name)
    return nets


def copper_counts(model):
    return (
        Counter(t.net_name for t in model.tracks if t.net_name),
        Counter(a.net_name for a in model.arcs if a.net_name),
        Counter(v.net_name for v in model.vias if v.net_name),
    )


def run_stage(model, tree, params, total_iters, step_iters, protected, tag):
    """Iterative placement with app accept semantics per step.

    Each step: run -> commit footprints + rip affected copper (model and
    tree) -> the next step simulates on the stripped board.
    """
    by_uuid = {fp.uuid: fp for fp in model.footprints if fp.uuid}
    used = 0
    while True:
        step_params = replace(params, max_iterations=step_iters)
        proposal = run_placement(model, step_params)
        used += proposal.iterations
        deltas = {
            u: d
            for u, d in proposal.deltas.items()
            if tuple(d) != (0.0, 0.0, 0.0) and tuple(d) != (0.0, 0.0)
        }
        if not deltas:
            print(f"[{tag}] no movement; stop={proposal.stop_reason}")
            break
        moved = set(deltas)
        nets = affected_nets(model, moved)
        tb, ab, vb = copper_counts(model)
        lab = dict(getattr(proposal, "label_deltas", {}) or {})
        model = commit_placement(model, deltas, protected, label_deltas=lab or None)
        tree = rip_up_nets(tree, nets, protected)
        for uuid, (dx, dy, da) in deltas.items():
            ldx, ldy = lab.get(uuid, (0.0, 0.0))
            tree = nudge_footprint_by_uuid(
                tree, uuid, dx, dy, da, label_dx_mm=ldx, label_dy_mm=ldy
            )
        ta, aa, va = copper_counts(model)
        ripped = {
            n: (
                tb.get(n, 0) - ta.get(n, 0),
                ab.get(n, 0) - aa.get(n, 0),
                vb.get(n, 0) - va.get(n, 0),
            )
            for n in sorted(nets - protected)
        }
        ripped = {n: c for n, c in ripped.items() if any(c)}
        n_tracks = sum(c[0] for c in ripped.values())
        print(
            f"[{tag}] moved={len(moved)} iters={proposal.iterations} "
            f"disp={proposal.final_max_disp_mm:.3f}mm stop={proposal.stop_reason} "
            f"residual={proposal.residual_max_force_N:.3g}N "
            f"ripped_tracks={n_tracks} nets={len(ripped)} "
            f"skipped_protected={sorted(nets & protected)}"
        )
        if proposal.stop_reason == "converged":
            print(f"[{tag}] converged")
            break
        if used >= total_iters:
            print(f"[{tag}] budget {used}/{total_iters}")
            break
    return model, tree, used


def drc_histogram(path):
    """(type -> count) for violations; unconnected items counted separately.

    Reads ``kicad-cli pcb drc`` JSON directly (instead of
    :func:`validate.validate_pcb`, which folds unconnected items into the
    violation list) so the triage can ignore unconnected nets by design.
    """
    import json
    import subprocess
    import tempfile
    import uuid as _uuid

    from kicad_autorouter.validate import find_kicad_cli

    try:
        cli = find_kicad_cli()
    except KicadCliNotFound as exc:
        print(f"warning: {exc}; skipping DRC for {path}")
        return None, None
    report_path = Path(tempfile.gettempdir()) / f"strip_{_uuid.uuid4().hex}.json"
    try:
        proc = subprocess.run(
            [cli, "pcb", "drc", str(path), "--format", "json", "-o", str(report_path)],
            capture_output=True, text=True, timeout=300,
        )
        load_error = next(
            (l for l in proc.stderr.splitlines() if "Failed to load" in l), None
        )
        if load_error or not report_path.exists():
            print(f"DRC: failed to load {path}: {load_error or proc.stderr.strip()}")
            return None, None
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"DRC: failed on {path}: {exc}")
        return None, None
    finally:
        try:
            report_path.unlink()
        except OSError:
            pass
    hist = Counter(v.get("type", "?") for v in data.get("violations", []))
    return hist, len(data.get("unconnected_items", []))


def report_triage(name, hist, base):
    print(f"--- DRC {name}: {sum(hist.values())} violations (unconnected ignored)")
    for t in sorted(set(hist) | set(base)):
        d = hist.get(t, 0) - base.get(t, 0)
        flag = "  <-- NEW" if base.get(t, 0) == 0 and hist.get(t, 0) else ""
        if d:
            print(f"    {t:30s} base={base.get(t,0):5d} now={hist.get(t,0):5d} ({d:+d}){flag}")


def main(argv=None) -> int:
    repo = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description="Cluster -> strip -> legalize pipeline.")
    p.add_argument(
        "input", nargs="?", default=str(repo / "tests" / "fixtures" / "tube111.kicad_pcb")
    )
    p.add_argument("--out", default=str(repo / "tmp" / "tube112.kicad_pcb"))
    p.add_argument("--legal-out", default=str(repo / "tmp" / "tube112_legal.kicad_pcb"))
    p.add_argument("--cluster-iters", type=int, default=150)
    p.add_argument("--legal-iters", type=int, default=500)
    p.add_argument("--step", type=int, default=50)
    p.add_argument("--no-protect", action="store_true",
                   help="rip ALL affected nets (no protected power/ground copper)")
    p.add_argument("--no-drc", action="store_true")
    args = p.parse_args(argv)

    tree = parse_file(Path(args.input))
    model = board_model(tree)
    protected = set() if args.no_protect else seed_protected(model)
    locked = sum(fp.locked for fp in model.footprints)
    print(f"loaded {args.input}: {len(model.footprints)} footprints ({locked} locked)")
    print(f"protected nets: {sorted(protected)}")

    base_hist, base_unconn = (Counter(), 0)
    if not args.no_drc:
        base_hist, base_unconn = drc_histogram(Path(args.input)) or (Counter(), 0)
        print(f"input baseline: {sum(base_hist.values())} violations, "
              f"{base_unconn} unconnected (ignored below)")

    # Stage A: cluster + auto-rip.
    cluster_params = PlacementParams.from_dict(CLUSTER)
    model, tree, used_c = run_stage(
        model, tree, cluster_params, args.cluster_iters, args.step, protected, "cluster"
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_sexpr(tree) + "\n", encoding="utf-8")
    print(f"saved stripped -> {out} ({used_c} cluster iters)")

    if not args.no_drc:
        hist, _ = drc_histogram(out) or (Counter(), 0)
        report_triage("stripped tube112", hist, base_hist)

    # Stage B: legalize the stripped board from disk (true intermediate state).
    tree2 = parse_file(out)
    model2 = board_model(tree2)
    protected2 = set() if args.no_protect else seed_protected(model2)
    legal_params = PlacementParams.from_dict(LEGALIZE)
    model2, tree2, used_l = run_stage(
        model2, tree2, legal_params, args.legal_iters, args.step, protected2, "legalize"
    )
    lout = Path(args.legal_out)
    lout.write_text(to_sexpr(tree2) + "\n", encoding="utf-8")
    print(f"saved legalized -> {lout} ({used_l} legalize iters)")

    if not args.no_drc:
        hist2, _ = drc_histogram(lout) or (Counter(), 0)
        report_triage("legalized tube112", hist2, base_hist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
