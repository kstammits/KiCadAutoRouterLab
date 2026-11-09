#!/usr/bin/env python3
"""Staged place -> strip -> legalize -> tidy -> rails -> signals pipeline.

Builds from a test fixture (default: tube111 worktree) through the app's
accept semantics (``BoardState.accept_proposal``), then strips ALL copper
(tracks, vias, arcs AND zone pours) up front so nothing can short — a
stripped board is expected to be full of unconnected nets by design:

    filed -> [strip all copper] -> [cluster 500] -> tmp/tube112.kicad_pcb
          -> [legalize 1000] -> tmp/tube113.kicad_pcb
          -> [tidy 200] -> tmp/tube114.kicad_pcb
          -> [rails only] -> tmp/tube115_rails.kicad_pcb
          -> [signals on top] -> tmp/tube116_routed.kicad_pcb

Each stage reloads the previous output from disk (true intermediate state)
and gets a DRC report saved next to it (``*.drc.json``). Triage ignores
``unconnected_items`` and diffs violation types against the input baseline.
Routing triage additionally diffs against the routing input (tube114) so
placement baggage and router-introduced copper errors stay separated.

Regime presets are copied from ``ui/app-params.js`` (Cluster scatters,
Legalize settles placed boards, Tidy snaps Manhattan + holds rows; see
``docs/si_force_laws.md`` section 3). Routing mirrors the UI Route-All
path: default ``RoutingParams`` with the board-mode overlay from
``strategy.apply_mode`` (``infer_mode`` on the board's copper layers),
power rails protected and routed first, then signals incrementally.

Runs under the project .venv (pure Python; only DRC needs `kicad-cli`):

    .venv/bin/python scripts/place_strip.py [--no-drc]
    .venv/bin/python scripts/place_strip.py tmp/tube114.kicad_pcb \\
        --stages rails signals [--no-drc]

Preset experiments are code edits (preset dicts / budgets below), never
CLI flags, so run logs always match the code. Variant A/B outputs are
copied aside manually; the tube112..116 names are fixed.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import (  # noqa: E402
    board_model,
    commit_placement,
)
from kicad_autorouter.io import (  # noqa: E402
    nudge_footprint_by_uuid,
    reset_label_anchors,
    rip_up_nets,
)
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402
from kicad_autorouter.sexpr import SExpr, parse_file, to_sexpr  # noqa: E402
from kicad_autorouter.validate import KicadCliNotFound  # noqa: E402

# Regime presets from ui/app-params.js (applyRegimePreset cluster/legalize/tidy).
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
    # Halved decap-chip pull (default 4.0): the U3 pocket held one victim
    # regardless of budget (C12 1.09 -> C13 0.78 @1000). Caps must still
    # hug their chips — the decap-gap metric in the triage gates this.
    "decap_link": 2.0,
    "wall_halo_m": 0.004,
    "cross_layer_pin_halo_m": 0.0007,
    "wall_exponent": 2.0,
    "convergence_eps_m": 2e-5,
    "align_angle_k_N": 0,
    "align_pos_k_N_m": 0,
    "align_deadband_deg": 10,
    "align_radius_m": 0.020,
}
TIDY = {
    "attraction_k_N": 0.002,
    "wall_k_N": 0.1,
    "damping_Ns_m": 0.12,
    "dt_s": 1e-3,
    "vmax_m_s": 1.0,
    "friction_N": 1e-5,
    "wall_halo_m": 0.004,
    "cross_layer_pin_halo_m": 0.0007,
    "wall_exponent": 2.0,
    "convergence_eps_m": 2e-5,
    "align_angle_k_N": 3e-3,
    "align_pos_k_N_m": 0.5,
    "align_deadband_deg": 10,
    "align_radius_m": 0.020,
}


# Stage outputs (fixed: the tube112..116 names are the contract — variant A/B
# outputs are copied aside manually) and iteration budgets (part of the
# presets: edit here so logs always match code, never via CLI).
OUT112 = "tube112.kicad_pcb"
OUT113 = "tube113.kicad_pcb"
OUT114 = "tube114.kicad_pcb"
OUT115 = "tube115_rails.kicad_pcb"
OUT116 = "tube116_routed.kicad_pcb"
CLUSTER_ITERS = 500
LEGAL_ITERS = 1000
TIDY_ITERS = 200
# Slice size above stall_window (100): per-slice stop reasons are honest
# (converged / stalled reachable), not budget-by-construction.
STEP_ITERS = 200


# Window-LED guidance: the five middle-board diodes (D1-D5; D6/D7 are
# last-board power inlet, untouched). One magnet per LED exactly under its
# filed tube111 position, so the window LEDs stay near their
# enclosure-window spots while the rest settles (a shared anchor would
# repeat the U3 decap-pocket mistake). Sizing: all placement forces here
# are constant-magnitude, so holders must out-pull the whole tug-of-war —
# cluster drags each 2-net LED with up to 2 x 50mN of net pull plus walls.
# ka = 4000 → ghost_k_N * ka = 200mN wins that sum with margin, in every
# stage. Attached in all placement stages (a ghost-free cluster scatters
# D2/D5 50-68mm first, and no constant magnet wins back 60mm of stretched
# net). Empty = off.
# Format: (ghost_ref, anchor_x_mm, anchor_y_mm, target_ref, ka).
LED_GHOSTS = (
    ("GHOST-D1", 79.51, 43.65, "D1", 4000),
    ("GHOST-D2", 100.15, 45.40, "D2", 4000),
    ("GHOST-D3", 100.10, 57.30, "D3", 4000),
    ("GHOST-D4", 78.64, 63.99, "D4", 4000),
    ("GHOST-D5", 78.55, 70.26, "D5", 4000),
)
# Pin contrast: session-immobile refs, frozen wherever the input file has
# them (movable_uuids exclusion). Empty = off. Unlike file-backed locks,
# pins are experiment config, not fixture facts.
PIN_REFS = ()


def led_ghosts():
    """Build window-LED ghost attractors (fixed uuids: deterministic runs)."""
    from kicad_autorouter.ghost import make_ghost_attractor

    return tuple(
        make_ghost_attractor(
            ref, x, y, target, ka, size_mm=1.0,
            uuid=f"ghost-led-{target.lower()}",
        )
        for ref, x, y, target, ka in LED_GHOSTS
    )


def pinned_uuids(model, pin_refs):
    """UUIDs for PIN_REFS entries (unknown refs warn and are skipped)."""
    by_ref = {fp.ref: fp.uuid for fp in model.footprints if fp.ref and fp.uuid}
    out = set()
    for ref in pin_refs:
        if ref in by_ref:
            out.add(by_ref[ref])
        else:
            print(f"warning: pin ref {ref!r} not found; skipping")
    return out


def attach_led_ghosts(model):
    """Append LED_GHOSTS attractors, inheriting each target's region.

    Ghosts without a footprint_region entry are skipped by the region
    simulation (region.py), so region-less ghosts are silently inert.
    Anchors are placed in-region by construction; the target's region is
    the correct assignment.
    """
    ghosts = led_ghosts()
    if not ghosts:
        return model
    region = dict(model.footprint_region)
    for g in ghosts:
        for target_ref, _ka in g.ghost_attractions:
            tgt = model.by_ref.get(target_ref)
            if tgt is not None and tgt.uuid:
                region[g.uuid] = model.footprint_region.get(tgt.uuid, -1)
                break
    return replace(
        model,
        ghost_footprints=model.ghost_footprints + ghosts,
        footprint_region=region)


def strip_all_copper(tree: SExpr, model) -> SExpr:
    """Remove every copper feature: tracks, arcs, vias AND zone pours.

    Keepout zones (if any) are preserved — they are constraints, not copper.
    After this the board cannot short; unconnected nets are expected.
    """
    nets = set()
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name:
                nets.add(pad.net_name)
    for t in model.tracks:
        if t.net_name:
            nets.add(t.net_name)
    for v in model.vias:
        if v.net_name:
            nets.add(v.net_name)
    tree = rip_up_nets(tree, nets, set())

    def keep(node) -> bool:
        if not isinstance(node, SExpr) or node.head != "zone":
            return True
        return any(
            isinstance(c, SExpr) and c.head == "keepout" for c in node.args
        )

    return replace(tree, args=tuple(c for c in tree.args if keep(c)))


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


def zone_count(tree: SExpr) -> int:
    return sum(
        1 for c in tree.args if isinstance(c, SExpr) and c.head == "zone"
    )


def run_stage(model, tree, params, total_iters, step_iters, protected, tag,
               pinned=()):
    """Iterative placement with app accept semantics per step.

    Each step: run -> commit footprints + rip affected copper (model and
    tree) -> the next step simulates on the stripped board. Pinned
    footprints (by uuid) are excluded from ``movable_uuids`` so they act
    as fixed anchors without file-backed locks.
    """
    by_uuid = {fp.uuid: fp for fp in model.footprints if fp.uuid}
    movable = {u for u, f in by_uuid.items() if not f.locked} - set(pinned)
    if pinned:
        print(f"[{tag}] pinned {len(set(pinned) & set(by_uuid))}: "
              f"{sorted(f.ref for f in model.footprints if f.uuid in set(pinned))}")
    used = 0
    while True:
        step_params = replace(params, max_iterations=step_iters)
        proposal = run_placement(model, step_params, movable_uuids=movable)
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


def drc_histogram(path, save_json=None):
    """(type -> count) for violations; unconnected items counted separately.

    Reads ``kicad-cli pcb drc`` JSON directly (instead of
    :func:`validate.validate_pcb`, which folds unconnected items into the
    violation list) so the triage can ignore unconnected nets by design.
    The raw report is saved to ``save_json`` when given.
    """
    import json
    import shutil
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
        if save_json is not None:
            shutil.copy(report_path, save_json)
            print(f"DRC report saved -> {save_json}")
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


def report_legality(name, model):
    """Engine legality on final poses (cheap, no kicad-cli).

    Routing stages must move no parts: the pair set here should equal the
    routing input's pair set exactly. Any delta means the driver committed
    deltas or the writeback shifted geometry.
    """
    from kicad_autorouter.placement.legality import check_legality

    rep = check_legality(model, {})
    refs = {fp.uuid: (fp.ref or "?") for fp in model.footprints}
    print(f"--- legality {name}: legal={rep.is_legal} "
          f"pairs={len(rep.overlapping_pairs)} max={rep.max_pair_overlap_mm2:.3f}mm2 "
          f"wall={rep.max_wall_penetration_mm:.3f}mm")
    for a, b, area in rep.overlapping_pairs:
        print(f"    {refs.get(a, a[:8])}-{refs.get(b, b[:8])} {area:.3f}")
    for u, d in rep.wall_violations:
        print(f"    wall {refs.get(u, u[:8])} {d:.3f}")


def net_pad_counts(model):
    """Pad count per net name (None-net pads excluded: never routable)."""
    counts = Counter()
    for fp in model.footprints:
        for pad in fp.pads:
            if pad.net_name:
                counts[pad.net_name] += 1
    return counts


def report_connectivity(name, model, nets, tol_mm=0.05):
    """Per-net copper connectivity: pads + track endpoints + vias in one DSU.

    A routed net must be a single component. Region-partition bridges,
    piecemeal chunk joins, or dangling stubs that fail to land all show up
    here as extra components — the routing-stage equivalent of the
    placement legality gate. Pure geometry, no kicad-cli.
    """
    print(f"--- connectivity {name} (tol={tol_mm}mm):")
    from shapely.geometry import LineString, box
    from shapely.ops import unary_union

    for net in sorted(nets):
        pad_polys = []
        for fp in model.footprints:
            for pad in fp.pads:
                if pad.net_name == net:
                    # Axis-aligned copper rect (tidy guarantees Manhattan
                    # movables; this fixture's locked hardware is 0/90 too).
                    hx, hy = pad.size_mm[0] / 2.0, pad.size_mm[1] / 2.0
                    px, py = pad.position.x_mm, pad.position.y_mm
                    pad_polys.append(box(px - hx, py - hy, px + hx, py + hy))
        lines = []
        for t in list(model.tracks) + list(model.arcs):
            if t.net_name == net:
                lines.append(LineString([
                    (t.start.x_mm, t.start.y_mm), (t.end.x_mm, t.end.y_mm)]))
                try:
                    lines.append(LineString([
                        (t.end.x_mm, t.end.y_mm), (t.mid.x_mm, t.mid.y_mm)]))
                except AttributeError:
                    pass
        for v in model.vias:
            if v.net_name == net:
                x, y = v.position.x_mm, v.position.y_mm
                pad_polys.append(box(x - 1e-6, y - 1e-6, x + 1e-6, y + 1e-6))
        copper = unary_union(
            [g.buffer(0.001, cap_style="flat") for g in lines] + pad_polys
        )
        parts = list(getattr(copper, "geoms", [copper]))
        touched = set()
        for pi, poly in enumerate(parts):
            for pp in pad_polys:
                if poly.intersects(pp):
                    touched.add(pi)
                    break
        ncomp = len(touched)
        flag = "" if ncomp <= 1 else "  <-- FRACTURED"
        print(f"    {net:22s} pad-components={ncomp}{flag}")


def run_route_stage(model, tree, net_names, protected, params, tag, extra_power=()):
    """Route one net list via the UI Route-All path (route_nets + writeback).

    Logs per-net pair completion (best-effort progress hook), run totals
    (copper, stitch, expansions, timings, wire), and every failed net with
    its explicit reason — the iteration surface for preset/engine work.
    Returns the re-parsed model, the new tree, and the RoutingResult.
    """
    from kicad_autorouter.routing.pipeline import route_nets

    seen = set()

    def progress_cb(net, pairs_done, pairs_total, expansions):
        # Log each net once at first completion: retries re-fire the hook
        # per pair and would spam thousands of lines on hard nets.
        if pairs_total and pairs_done >= pairs_total and net not in seen:
            print(f"[{tag}] net {net}: {pairs_done}/{pairs_total} pairs exp={expansions}")
            seen.add(net)

    t0 = time.time()
    result, _, _ = route_nets(
        model=model,
        net_names=list(net_names),
        protected_nets=set(protected),
        params=params,
        original_pcb_tree=tree,
        extra_power_nets=tuple(extra_power),
        progress_cb=progress_cb,
    )
    dt = time.time() - t0
    drc_state = ("clean" if result.drc_clean else "DIRTY") if params.run_drc else "n/a"
    print(f"[{tag}] routed={result.nets_routed} failed={result.nets_failed} "
          f"cancelled={result.cancelled} tracks+={result.tracks_added} "
          f"vias+={result.vias_added} arcs+={result.arcs_added} "
          f"stitch_v+={result.stitch_vias_added} stitch_t+={result.stitch_tracks_added} "
          f"gnd_islands={result.gnd_islands_before}->{result.gnd_islands_after} "
          f"gnd_stripped={result.gnd_stripped} expansions={result.expansions} "
          f"wire={result.total_wire_mm:.0f}mm time={dt:.1f}s drc={drc_state}")
    for stage, val in (result.timings or {}).items():
        try:
            print(f"[{tag}] timing {stage}: {float(val):.2f}s")
        except (TypeError, ValueError):
            print(f"[{tag}] timing {stage}: {val}")
    for net, reason in (result.failed_reasons or {}).items():
        print(f"[{tag}] FAILED {net}: {reason}")
    new_tree = result.pcb_tree if result.pcb_tree is not None else tree
    return board_model(new_tree), new_tree, result


def main(argv=None) -> int:
    repo = Path(__file__).resolve().parent.parent
    tmp = repo / "tmp"
    p = argparse.ArgumentParser(
        description="Cluster -> strip -> legalize -> tidy -> rails -> signals pipeline.")
    p.add_argument(
        "input", nargs="?", default=str(repo / "tests" / "fixtures" / "tube111.kicad_pcb")
    )
    p.add_argument("--stages", nargs="+",
                   default=["cluster", "legalize", "tidy", "rails", "signals"],
                   choices=("cluster", "legalize", "tidy", "rails", "signals"),
                   help="stages to run, in canonical order (default: all). Each "
                        "stage reloads its input from disk: the first selected "
                        "stage reads [input], later stages read the previous "
                        "stage's fixed output.")
    p.add_argument("--no-drc", action="store_true")
    args = p.parse_args(argv)
    input_path = args.input
    selected = [s for s in ("cluster", "legalize", "tidy", "rails", "signals")
                if s in set(args.stages)]
    protected: set = set()  # full strip: nothing is protected from rip-up

    tree = parse_file(Path(input_path))
    model = board_model(tree)
    filed_tree = tree  # label-anchor source: the human-filed board
    locked = sum(fp.locked for fp in model.footprints)
    print(f"loaded {input_path}: {len(model.footprints)} footprints ({locked} locked)")

    base_hist = Counter()
    if any(s in selected for s in ("cluster", "legalize", "tidy")):
        print(f"placement stages from {input_path}")
        if not args.no_drc and "cluster" in selected:
            base_hist, base_unconn = drc_histogram(Path(input_path)) or (Counter(), 0)
            print(f"input baseline: {sum(base_hist.values())} violations, "
                  f"{base_unconn} unconnected (ignored below)")

        if "cluster" in selected:
            # Full copper strip up front: tracks, vias, arcs, and zone pours.
            # Later stages start from stripped/clustered boards, never filed.
            n_tracks = len(model.tracks)
            tree = strip_all_copper(tree, model)
            model = board_model(tree)
            print(f"stripped all copper: {n_tracks} tracks + pours removed, "
                  f"{len(model.tracks)} tracks / {zone_count(tree)} zones remain")

        stages = [
            ("cluster", CLUSTER, CLUSTER_ITERS, tmp / OUT112),
            ("legalize", LEGALIZE, LEGAL_ITERS, tmp / OUT113),
            ("tidy", TIDY, TIDY_ITERS, tmp / OUT114),
        ]
        ran = False
        for i, (tag, preset, budget, out) in enumerate(stages):
            if tag not in selected:
                continue
            if ran:
                # Reload the previous stage output: true intermediate state.
                # (The first selected stage runs on [input], loaded above.)
                prev = stages[i - 1][3]
                tree = parse_file(prev)
                model = board_model(tree)
            ran = True
            # Rebase label anchors to filed before every stage: the orbit
            # tether + clamp would otherwise compound up to 5mm of drift
            # per stage. No-op on the filed input itself.
            tree, n_reset = reset_label_anchors(tree, filed_tree)
            if n_reset:
                model = board_model(tree)
                print(f"[{tag}] rebased {n_reset} label anchors to filed")
            if tag in ("cluster", "legalize", "tidy") and LED_GHOSTS:
                model = attach_led_ghosts(model)
                print(f"[{tag}] attached {len(LED_GHOSTS)} LED ghosts")
            params = PlacementParams.from_dict(preset)
            model, tree, used = run_stage(
                model, tree, params, budget, STEP_ITERS, protected, tag,
                pinned_uuids(model, PIN_REFS))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(to_sexpr(tree) + "\n", encoding="utf-8")
            print(f"saved {tag} -> {out} ({used} iters)")
            if not args.no_drc:
                drc_json = out.with_name(out.stem + ".drc.json")
                hist, _ = drc_histogram(out, save_json=drc_json) or (Counter(), 0)
                if base_hist:
                    report_triage(f"{tag} {out.name}", hist, base_hist)
                else:
                    # Subset run without the filed baseline: counts only,
                    # no NEW flags (there is nothing to diff against).
                    print(f"--- DRC {tag} {out.name}: {sum(hist.values())} "
                          f"violations (no baseline in subset run)")

    if any(s in selected for s in ("rails", "signals")):
        from kicad_autorouter.routing.pipeline import RoutingParams
        from kicad_autorouter.routing.power import identify_power_nets
        from kicad_autorouter.strategy import apply_mode, infer_mode

        mode = infer_mode(model.copper_layers)
        route_params = RoutingParams.from_dict(apply_mode({}, mode))
        if "signals" in selected:
            # The signals stage always runs on top of committed 115 rails:
            # the two-layer preset's GND-blocker eviction would rip that
            # copper out from under us (the stitcher rebuilds pour
            # islands, not evicted routes). Force it off.
            if route_params.strip_gnd_blockers:
                route_params = replace(route_params, strip_gnd_blockers=False)
                print("signals on existing copper: forcing strip_gnd_blockers=false")
        print(f"routing mode={mode} layers={list(model.copper_layers)} "
              f"grid={route_params.grid_resolution_mm}mm "
              f"track={route_params.track_width_mm}/power={route_params.power_width_mm}mm "
              f"via={route_params.via_size_mm}/{route_params.via_drill_mm}mm "
              f"arc_fillets={route_params.arc_fillets}")
        # Rails input: the tidy output when tidy ran just now, else [input].
        routing_input_path = tmp / OUT114 if "tidy" in selected else Path(input_path)
        route_base = Counter()
        if not args.no_drc:
            route_base, _ = drc_histogram(routing_input_path) or (Counter(), 0)
        counts = net_pad_counts(model)
        power, ground, _ = identify_power_nets(model, ())
        rails = [n for n in list(ground) + list(power) if counts.get(n, 0) >= 2]
        routable = [n for n in model.nets if counts.get(n, 0) >= 2]
        signals = [n for n in routable if n not in set(rails)]
        print(f"nets: {len(routable)} routable ({len(counts)} with pads); "
              f"rails={rails} signals={len(signals)} "
              f"skipped_1pad={[n for n in counts if counts[n] < 2]}")

        def save_route(tag, new_tree, out_arg):
            out = Path(out_arg)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(to_sexpr(new_tree) + "\n", encoding="utf-8")
            print(f"saved {tag} -> {out}")
            new_model = board_model(new_tree)
            report_legality(f"{tag} {out.name}", new_model)
            routed_here = rails if tag == "rails" else signals
            report_connectivity(f"{tag} {out.name}", new_model, routed_here)
            if not args.no_drc:
                drc_json = out.with_name(out.stem + ".drc.json")
                hist, unconn = drc_histogram(out, save_json=drc_json) or (Counter(), 0)
                print(f"--- unconnected {tag} {out.name}: {unconn} items")
                report_triage(f"{tag} {out.name} vs routing input", hist, route_base)

        if "rails" in selected:
            model, tree, _ = run_route_stage(
                model, tree, rails, set(rails), route_params, "rails")
            save_route("rails", tree, tmp / OUT115)
        if "signals" in selected:
            if "rails" not in selected:
                # Reload the rails output: true intermediate state.
                tree = parse_file(tmp / OUT115)
                model = board_model(tree)
            model, tree, _ = run_route_stage(
                model, tree, signals, set(rails), route_params, "signals")
            save_route("signals", tree, tmp / OUT116)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
