#!/usr/bin/env python3
"""Wall-hardening sweep: exponent x force scale x deep-cap.

Empirical sweep for wall-law tuning: does a nonlinear wall (p > 1,
optionally rescaled and depth-capped) hold parts out of locked courtyards at
shallower depths without breaking clustering, rotation, or stability?

Cells:
  A. head-on press probe (2-3 bodies, analytic equilibrium known):
     p x ka(opposing pull) x cap -> equilibrium gap/penetration vs theory,
     plus pass-through detection when the cap saturates below the pull.
  B. R13/RV1 isolated trap probe (locked RV1 + resistor + east/west
     anchors mimicking the 3-vs-2 edge asymmetry): final pose + overlap.
  C. full DCCF + tube111 spot: overlap pair-set/area, rotation, stop stats.

Usage:
    .venv/bin/python scripts/sweep_wall_hardening.py [--out docs/benchmarks/wall_hardening_<date>.json]
    Prints a markdown table body to stdout; writes JSON to --out.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from kicad_autorouter.board_model import BoardModel, Point, apply_deltas, board_model
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.sexpr import parse_file
from tests.helpers import make_fp

K0 = 0.08
HALO_MM = 6.0


def rescaled_k(p: float) -> float:
    """Match F at one-halo penetration across p: k(p) = 2*K0/2^p."""
    return 2.0 * K0 / (2.0 ** p)


def theory_balance_s_mm(F_N: float, k_N: float, p: float,
                        cap_mm: float = 0.0) -> float | None:
    """Signed separation where wall balances a head-on pull F (mm).

    Halo branch (F <= k): s = h*(1-(F/k)^(1/p)) — higher p rests CLOSER.
    Penetration branch (F > k): pen = h*((F/k)^(1/p)-1) — higher p rests
    SHALLOWER. Returns None when the (possibly capped) wall can never hold
    F (pass-through: part pushed through).
    """
    f_sat = k_N * ((1.0 + cap_mm / HALO_MM) ** p) if cap_mm > 0 else math.inf
    if F_N > f_sat:
        return None
    if F_N <= k_N:
        return HALO_MM * (1.0 - (F_N / k_N) ** (1.0 / p))  # halo gap
    return -HALO_MM * ((F_N / k_N) ** (1.0 / p) - 1.0)  # penetration


def _board(fps, edge=100.0):
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
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


def press_board(ka: float):
    """Mover east of a locked wall, one net edge reeling it west into it."""
    wall = make_fp("WALL", "uuid-wall", -15.0, 0.0, ("WX", "WY"),
                   footprint_id="test:W", hw=15.0, hh=10.0,
                   pad_layers=("F.Cu",), locked=True)
    mover = make_fp("M", "uuid-m", 12.0, 0.0, ("PULL", "FREE"),
                    hw=1.5, hh=1.0, pad_layers=("F.Cu",))
    anchor = make_fp("ANC", "uuid-anc", -40.0, 0.0, ("PULL", "ANC2"),
                     footprint_id="test:A", hw=1.5, hh=1.0,
                     pad_layers=("F.Cu",), locked=True)
    return _board([wall, mover, anchor], edge=100.0), ka


def press_equilibrium(model, prop) -> float | None:
    """Mover's west-edge signed separation vs the wall's east edge (mm).

    mover_west - wall_east directly (no intersection-width saturation, so
    deep equilibria read exactly). Positive = gap, negative = penetration.
    None if the mover centroid crossed clean through to the far side.
    """
    from kicad_autorouter.placement import _collect_pad_nodes, _footprint_courtyard_polygon
    moved = apply_deltas(model, prop.deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    polys = {}
    for uuid in ("uuid-wall", "uuid-m"):
        fp = next(f for f in moved.footprints if f.uuid == uuid)
        polys[uuid] = _footprint_courtyard_polygon(
            fp, pad_positions=pos, pad_indices=uuid_to_idx.get(uuid, []))
    wall, mover = polys["uuid-wall"], polys["uuid-m"]
    cx = (mover.bounds[0] + mover.bounds[2]) / 2.0
    if cx < wall.bounds[0]:
        return None  # centroid through the far side
    return mover.bounds[0] - wall.bounds[2]


def board_overlap_stats(model, prop, thresh: float = 1.0):
    from kicad_autorouter.placement import _collect_pad_nodes, _footprint_courtyard_polygon
    moved = apply_deltas(model, prop.deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    polys = {}
    for fp in moved.footprints:
        if fp.ghost or not fp.pads or not fp.ref or fp.ref.startswith("G"):
            continue
        poly = _footprint_courtyard_polygon(
            fp, pad_positions=pos, pad_indices=uuid_to_idx.get(fp.uuid, []))
        if not poly.is_empty:
            polys[fp.ref] = poly
    names = list(polys)
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            area = polys[a].intersection(polys[b]).area
            if area > thresh:
                pairs.append((round(area, 1), a, b))
    pairs.sort(reverse=True)
    max_rot = 0.0
    for _, _, da in prop.deltas.values():
        max_rot = max(max_rot, abs(da))
    return pairs, round(max_rot, 2)


def run_cell(model, params: PlacementParams, label: str) -> dict:
    t0 = time.perf_counter()
    prop = run_placement(model, params)
    el = time.perf_counter() - t0
    finite = all(math.isfinite(v) for d in prop.deltas.values() for v in d)
    return {"prop": prop, "elapsed_s": round(el, 1), "finite": finite, "label": label}


def plot_findings(cells: list, outdir: pathlib.Path, dpi: int = 150) -> pathlib.Path:
    """Empirical finding figures from sweep cells (press theory-vs-measured
    + full-board wedge depth vs p). Regenerable: rerun this script, then
    call plot_findings on the written JSON."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    # (a) press probe ka=0.2, uncapped: measured vs theory across p.
    press = [c for c in cells if c["family"] == "press"
             and c["ka_N"] == 0.2 and c["cap_mm"] == 0.0]
    press.sort(key=lambda c: c["p"])
    ps = [c["p"] for c in press]
    axes[0].plot(ps, [c["s_pred_mm"] for c in press], "o--", label="theory")
    axes[0].plot(ps, [c["s_meas_mm"] for c in press], "s-", label="measured")
    axes[0].axhline(0, color="k", linestyle=":", alpha=0.5)
    axes[0].set_xlabel("wall exponent p")
    axes[0].set_ylabel("equilibrium s* (mm, gap + / penetration -)")
    axes[0].set_title("Head-on press (0.2N pull): theory vs measured")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)
    # (b) full-board wedge/area vs p (same-k, uncapped).
    for fam, marker in (("dccf", "o"), ("tube111", "s")):
        sub = [c for c in cells if c["family"] == fam and c.get("klabel") in ("same-k", None)
               and c.get("cap_mm", 0.0) == 0.0
               and (fam != "dccf" or c.get("preset") == "defaults")]
        sub.sort(key=lambda c: c["p"])
        if sub:
            axes[1].plot([c["p"] for c in sub],
                         [c["total_area_mm2"] for c in sub],
                         marker + "-", label=f"{fam} total area")
    dccf = [c for c in cells if c["family"] == "dccf" and c.get("preset") == "defaults"
            and c.get("klabel") == "same-k" and c.get("cap_mm", 0.0) == 0.0]
    dccf.sort(key=lambda c: c["p"])
    if dccf:
        axes[1].plot([c["p"] for c in dccf], [c["rv1_r13_mm2"] for c in dccf],
                     "^-", label="DCCF RV1/R13 area")
    axes[1].set_xlabel("wall exponent p")
    axes[1].set_ylabel("overlap area (mm²)")
    axes[1].set_title("Full-board overlap vs p (same k, uncapped)")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)
    fig.suptitle("Wall-Hardening Sweep: empirical findings")
    fig.tight_layout()
    dest = outdir / "fig_wall_sweep.png"
    fig.savefig(dest, dpi=dpi)
    plt.close(fig)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=f"docs/benchmarks/wall_hardening_{time.strftime('%Y-%m-%d')}.json")
    ap.add_argument("--dccf-iters", type=int, default=200)
    ap.add_argument("--skip-full", action="store_true", help="skip full-board cells")
    ap.add_argument("--plot-only", default=None,
                    help="skip runs; regenerate findings figure from this JSON")
    args = ap.parse_args(argv)

    if args.plot_only:
        src = pathlib.Path(args.plot_only)
        data = json.loads(src.read_text())
        outdir = REPO_ROOT / "docs/figures"
        outdir.mkdir(parents=True, exist_ok=True)
        dest = plot_findings(data["cells"], outdir)
        print(f"wrote {dest.relative_to(REPO_ROOT)}")
        return 0

    out: dict = {"cells": []}
    base_kw = dict(convergence_eps_m=1e-12, repulsion_k_N_m2=1e-15,
                   ghost_k_N=1e-15, damping_Ns_m=0.12,
                   dt_s=1e-3, friction_N=1e-6)

    # ---- A. head-on press probe (analytic ground truth) ----
    # ka=0.2 cells need a longer budget: the crawl into deep equilibrium
    # is slow (dt-floor stepping under bleed), 300 iters stops mid-course.
    for p in (1.0, 1.5, 2.0, 3.0):
        for ka in (0.05, 0.2):
            iters = 300 if ka < 0.1 else 1500
            for cap in (0.0, 6.0, 12.0):
                model, _ = press_board(ka)
                params = PlacementParams(max_iterations=iters, attraction_k_N=ka,
                                         wall_k_N=K0, wall_halo_m=0.006,
                                         wall_exponent=p, wall_cap_depth_mm=cap,
                                         **base_kw)
                r = run_cell(model, params, f"press p={p} ka={ka} cap={cap}")
                prop = r["prop"]
                s_meas = press_equilibrium(model, prop)
                s_pred = theory_balance_s_mm(ka, K0, p, cap)
                out["cells"].append({
                    "family": "press", "p": p, "ka_N": ka, "k_N": K0,
                    "cap_mm": cap, "iters": prop.iterations,
                    "stop": prop.stop_reason,
                    "residual_N": round(prop.residual_max_force_N, 6),
                    "s_meas_mm": (None if s_meas is None else round(s_meas, 3)),
                    "s_pred_mm": s_pred, "finite": r["finite"],
                    "elapsed_s": r["elapsed_s"],
                })

    # ---- B. isolated trap probe ----
    # Dropped: small synthetics with hand-placed anchors escape around the
    # span ends (0 overlap at every p) instead of reproducing DCCF's trap,
    # which needs the exact neighbor field + net topology. The full-DCCF
    # cells below carry the real trap signal (rv1_r13_mm2); the press probe
    # above carries the analytic signal. No fake-discriminating family kept.

    # ---- C. full boards ----
    if not args.skip_full:
        dccf = board_model(parse_file(REPO_ROOT / "tests/fixtures/DCCF.sved.kicad_pcb"))
        presets = {
            "defaults": dict(attraction_k_N=0.008, wall_k_N=0.08, damping_Ns_m=0.12,
                             dt_s=1e-3),
            "legalize": dict(attraction_k_N=0.008, wall_k_N=0.09, damping_Ns_m=0.13,
                             dt_s=1e-3),
        }
        for preset, kw in presets.items():
            for p in (1.0, 2.0, 3.0):
                for k, klabel in ((kw["wall_k_N"], "same-k"),
                                  (2 * kw["wall_k_N"] / (2.0 ** p), "rescaled")):
                    params = PlacementParams(
                        max_iterations=args.dccf_iters, convergence_eps_m=2e-5,
                        wall_exponent=p, wall_cap_depth_mm=0.0,
                        wall_k_N=k, **{kk: vv for kk, vv in kw.items()
                                       if kk != "wall_k_N"})
                    r = run_cell(dccf, params, f"dccf/{preset} p={p} {klabel}")
                    prop = r["prop"]
                    pairs, max_rot = board_overlap_stats(dccf, prop)
                    cell = {"family": "dccf", "preset": preset, "p": p,
                            "k_N": round(k, 5), "klabel": klabel, "cap_mm": 0.0,
                            "iters": prop.iterations, "stop": prop.stop_reason,
                            "residual_N": round(prop.residual_max_force_N, 6),
                            "npairs": len(pairs),
                            "total_area_mm2": round(sum(a for a, _, _ in pairs), 1),
                            "pairs": [[a, x, y] for a, x, y in pairs[:12]],
                            "max_rot_deg": max_rot, "finite": r["finite"],
                            "elapsed_s": r["elapsed_s"]}
                    rv = next((a for a, x, y in pairs if {x, y} == {"RV1", "R13"}), 0.0)
                    cell["rv1_r13_mm2"] = rv
                    out["cells"].append(cell)
        # capped variants at the most promising corner (p=2/3, same-k, defaults)
        for p in (2.0, 3.0):
            params = PlacementParams(
                max_iterations=args.dccf_iters, convergence_eps_m=2e-5,
                attraction_k_N=0.008, wall_k_N=0.08, damping_Ns_m=0.12,
                dt_s=1e-3, wall_exponent=p, wall_cap_depth_mm=6.0)
            r = run_cell(dccf, params, f"dccf/defaults p={p} same-k cap=6")
            prop = r["prop"]
            pairs, max_rot = board_overlap_stats(dccf, prop)
            out["cells"].append({
                "family": "dccf", "preset": "defaults", "p": p, "k_N": 0.08,
                "klabel": "same-k", "cap_mm": 6.0, "iters": prop.iterations,
                "stop": prop.stop_reason,
                "residual_N": round(prop.residual_max_force_N, 6),
                "npairs": len(pairs),
                "total_area_mm2": round(sum(a for a, _, _ in pairs), 1),
                "pairs": [[a, x, y] for a, x, y in pairs[:12]],
                "max_rot_deg": max_rot, "finite": r["finite"],
                "elapsed_s": r["elapsed_s"],
                "rv1_r13_mm2": next((a for a, x, y in pairs if {x, y} == {"RV1", "R13"}), 0.0),
            })
        tube = board_model(parse_file(REPO_ROOT / "tests/fixtures/tube111.kicad_pcb"))
        params = PlacementParams(max_iterations=200, convergence_eps_m=2e-5,
                                 wall_exponent=2.0)
        r = run_cell(tube, params, "tube111 defaults p=2")
        prop = r["prop"]
        pairs, max_rot = board_overlap_stats(tube, prop)
        out["cells"].append({
            "family": "tube111", "p": 2.0, "iters": prop.iterations,
            "stop": prop.stop_reason,
            "residual_N": round(prop.residual_max_force_N, 6),
            "npairs": len(pairs),
            "total_area_mm2": round(sum(a for a, _, _ in pairs), 1),
            "max_rot_deg": max_rot, "finite": r["finite"],
            "elapsed_s": r["elapsed_s"],
        })

    dest = REPO_ROOT / args.out
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1) + "\n")
    try:
        shown = str(dest.relative_to(REPO_ROOT))
    except ValueError:
        shown = str(dest)
    # Markdown table body to stdout for the findings document.
    print("| cell | iters/stop | metric | residual |")
    print("|---|---|---|---|")
    for c in out["cells"]:
        if c["family"] == "press":
            metric = ("THROUGH" if c["s_meas_mm"] is None
                      else f"s={c['s_meas_mm']} vs pred {c['s_pred_mm']}")
            print(f"| press p={c['p']} ka={c['ka_N']} cap={c['cap_mm']} "
                  f"| {c['iters']}/{c['stop']} | {metric} | {c['residual_N']} |")
        else:
            print(f"| {c['family']}/{c.get('preset', '')} p={c['p']} "
                  f"{c.get('klabel', '')} cap={c.get('cap_mm', 0)} "
                  f"| {c['iters']}/{c['stop']} | npairs={c['npairs']} "
                  f"area={c['total_area_mm2']} rv1r13={c.get('rv1_r13_mm2', '?')} "
                  f"maxrot={c['max_rot_deg']} | {c['residual_N']} |")
    print(f"\nwrote {shown} ({len(out['cells'])} cells)")
    outdir = REPO_ROOT / "docs/figures"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        fdest = plot_findings(out["cells"], outdir)
        print(f"wrote {fdest.relative_to(REPO_ROOT)}")
    except Exception as exc:  # figures must never fail the sweep
        print(f"findings figure skipped: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
