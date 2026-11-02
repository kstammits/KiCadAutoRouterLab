#!/usr/bin/env python3
"""Force-balance figures for the DCCF long-budget regrowth item.

Two graphs, both from the live placement engine (no hand-tuned curves
except the wall-law theory overlay, which uses the same `_wall_mag` the
solver integrates). All runs use the engine-default repulsion (2e-9) —
earlier probes neutered it to 1e-15 following the sweep script, which
hid Coulomb from the balance entirely:

  fig_regrow_wall_sweep.png  Force balance vs parameter (2x2):
      (a) dense wall_k_N sweep (0.05–0.16) at 1000 iters, with chaos bars
      (same k at 900/1100 iters — the engine is deterministic, so spread
      is deterministic chaos from budget-truncated churn, not noise);
      (b) final-pose force magnitudes on U2 per cause vs wall_k_N;
      (c) halo sweep (2–8 mm) at k=0.1: areas plus a pressure metric
      (share of movable parts in wall contact, mean wall force);
      (d) repulsion scales (neutered 1e-15 / default 2e-9 / 10x 2e-8)
      at k=0.1: areas plus the U2 force split.
  fig_r11u2_force_balance.png  Force balance vs geometry, both sides of
      the wedge: U2 translated toward locked RV1 as well as toward R11
      about the regrown (k=0.1, 1000-iter) pose, static-probed. Log
      forces with theory overlay, a linear net-force inset marking the
      zero crossing, shaded overlap region, and the pair-bar intercept.
      Repulsion (<=1e-8 N at this geometry) is noted, not plotted.

Usage:
    python scripts/plot_regrowth_forces.py [--outdir docs/figures] [--dpi 150]
    python scripts/plot_regrowth_forces.py --only sweep
    python scripts/plot_regrowth_forces.py --only balance
    python scripts/plot_regrowth_forces.py --list

Costs ~15 min (27 DCCF x1000 runs + ~50 static probes).
"""

from __future__ import annotations

import argparse
import math
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
import numpy as np

from kicad_autorouter.board_model import apply_deltas, board_model
from kicad_autorouter.placement import (
    PlacementParams,
    _collect_pad_nodes,
    _footprint_courtyard_polygon,
    _wall_mag,
    run_placement,
)
from kicad_autorouter.placement.force import _overlap_depth_mm
from kicad_autorouter.sexpr import parse_file

FIGURES = ("sweep", "balance")

R11 = "a18a9dc4-c961-4422-ba2e-afd65cf4cae1"
U2 = "ef4533db-6ea4-4b68-b436-8e9575be570d"
RV1 = "3d552623-2969-4b15-8623-368144f225e9"

# Engine defaults for everything except the swept axis (repulsion is the
# honest 2e-9 default now, not the sweep script's neutered 1e-15).
BASE_KW = dict(max_iterations=1000, convergence_eps_m=2e-5,
               wall_halo_m=0.004, attraction_k_N=0.008,
               wall_k_N=0.1, wall_exponent=2.0, wall_cap_depth_mm=0.0,
               damping_Ns_m=0.12, dt_s=1e-3, repulsion_k_N_m2=2e-9,
               ghost_k_N=1e-15, friction_N=1e-6)


def _dccf():
    return board_model(parse_file(REPO_ROOT / "tests/fixtures/DCCF.sved.kicad_pcb"))


def _posed_polys(model):
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(model)
    pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
    polys = {}
    for fp in model.footprints:
        if fp.ghost or not fp.pads or not fp.ref or fp.ref.startswith("G"):
            continue
        poly = _footprint_courtyard_polygon(
            fp, pad_positions=pos, pad_indices=uuid_to_idx.get(fp.uuid, []))
        if not poly.is_empty:
            polys[fp.ref] = poly
    return polys


def _pair_areas(model, thresh=1.0):
    polys = _posed_polys(model)
    found = {}
    refs = list(polys)
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            area = polys[a].intersection(polys[b]).area
            if area > thresh:
                found[tuple(sorted((a, b)))] = area
    return found


def _mag(v) -> float:
    return float(math.hypot(v[0], v[1]))


def _pressure(prop, model):
    """Share of movable parts in wall contact + mean wall force (halo load)."""
    by_uuid = {fp.uuid: fp for fp in model.footprints}
    mags = []
    for uuid, comp in prop.force_components.items():
        fp = by_uuid.get(uuid)
        if fp is None or fp.locked or fp.ghost:
            continue
        mags.append(_mag(comp.get("courtyard", (0.0, 0.0))))
    if not mags:
        return 0.0, 0.0
    return (sum(1 for m in mags if m > 1e-9) / len(mags),
            sum(mags) / len(mags))


def run_cell(model, label, **kw):
    params = PlacementParams(**{**BASE_KW, **kw})
    prop = run_placement(model, params)
    moved = apply_deltas(model, prop.deltas)
    final = _pair_areas(moved)
    comps = prop.force_components.get(U2, {})
    frac, mean = _pressure(prop, model)
    cell = {"label": label, "k": params.wall_k_N,
            "halo_mm": params.wall_halo_m * 1000.0,
            "rep": params.repulsion_k_N_m2,
            "iters": prop.iterations, "stop": prop.stop_reason,
            "residual_N": prop.residual_max_force_N,
            "total_area": round(sum(final.values()), 1),
            "r11_u2": round(final.get(("R11", "U2"), 0.0), 2),
            "rv1_u2": round(final.get(("RV1", "U2"), 0.0), 2),
            "c19_u4": round(final.get(("C19", "U4"), 0.0), 2),
            "rv1_u1": round(final.get(("RV1", "U1"), 0.0), 2),
            "f_courtyard": _mag(comps.get("courtyard", (0.0, 0.0))),
            "f_attraction": _mag(comps.get("attraction", (0.0, 0.0))),
            "f_repulsion": _mag(comps.get("repulsion", (0.0, 0.0))),
            "contact_frac": frac, "mean_wall_N": mean}
    print(f"{label:16s} iters={prop.iterations}/{prop.stop_reason} "
          f"area={cell['total_area']} r11/u2={cell['r11_u2']} "
          f"rv1/u2={cell['rv1_u2']} c19/u4={cell['c19_u4']} "
          f"f_cy={cell['f_courtyard']:.4f} f_at={cell['f_attraction']:.4f} "
          f"f_rp={cell['f_repulsion']:.2e} p={frac:.2f}", flush=True)
    return cell


def plot_sweep(ks_cells, chaos, halo_cells, rep_cells, dest, dpi):
    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(14, 10))
    ks = [c["k"] for c in ks_cells]
    ax1.plot(ks, [c["total_area"] for c in ks_cells], "o-", label="total >1mm²")
    ax1.plot(ks, [c["r11_u2"] for c in ks_cells], "s-", label="R11/U2")
    ax1.plot(ks, [c["rv1_u2"] for c in ks_cells], "^-", label="RV1/U2")
    ax1.plot(ks, [c["c19_u4"] for c in ks_cells], "d-", label="C19/U4")
    for k, lo, hi in chaos:
        ax1.plot([k, k], [lo, hi], color="gray", lw=3, alpha=0.6, solid_capstyle="round")
    ax1.plot([], [], color="gray", lw=3, alpha=0.6, label="900–1100 iter spread")
    ax1.axhline(1.0, color="k", linestyle=":", alpha=0.5, label="pair bar (1mm²)")
    ax1.set_xlabel("wall_k_N (N)")
    ax1.set_ylabel("overlap area (mm²)")
    ax1.set_title("(a) DCCF @1000 iters: overlap vs wall strength")
    ax1.legend(fontsize=8)
    ax1.grid(True, alpha=0.3)
    ax2.plot(ks, [c["f_courtyard"] for c in ks_cells], "o-", label="courtyard on U2")
    ax2.plot(ks, [c["f_attraction"] for c in ks_cells], "s-", label="attraction on U2")
    ax2.plot(ks, [c["f_repulsion"] for c in ks_cells], "^-", label="repulsion on U2")
    ax2.set_xlabel("wall_k_N (N)")
    ax2.set_ylabel("force magnitude (N, log)")
    ax2.set_yscale("log")
    ax2.set_title("(b) Final-pose force balance on U2 vs wall strength")
    ax2.legend(fontsize=8)
    ax2.grid(True, alpha=0.3, which="both")
    hs = [c["halo_mm"] for c in halo_cells]
    ax3.plot(hs, [c["total_area"] for c in halo_cells], "o-", label="total >1mm²")
    ax3.plot(hs, [c["r11_u2"] for c in halo_cells], "s-", label="R11/U2")
    ax3.plot(hs, [c["rv1_u1"] for c in halo_cells], "x-", label="RV1/U1")
    ax3.axhline(1.0, color="k", linestyle=":", alpha=0.5)
    ax3.set_xlabel("halo (mm)")
    ax3.set_ylabel("overlap area (mm²)")
    ax3.set_title("(c) DCCF @1000 iters, k=0.1: overlap vs halo reach")
    ax3.legend(fontsize=8)
    ax3.grid(True, alpha=0.3)
    ax3b = ax3.twinx()
    ax3b.plot(hs, [c["mean_wall_N"] for c in halo_cells], "d--", color="C3",
              label="mean wall force / part")
    ax3b.set_ylabel("mean wall force per movable part (N, log)", color="C3")
    ax3b.set_yscale("log")
    ax3b.legend(fontsize=8, loc="center right")
    labels = ["neutered\n1e-15", "default\n2e-9", "10x\n2e-8"]
    xs = range(len(rep_cells))
    ax4.bar([x - 0.2 for x in xs],
            [c["total_area"] for c in rep_cells], 0.4, label="total >1mm²")
    ax4.bar([x + 0.2 for x in xs],
            [c["r11_u2"] for c in rep_cells], 0.4, label="R11/U2")
    ax4.axhline(1.0, color="k", linestyle=":", alpha=0.5)
    ax4.set_xticks(list(xs))
    ax4.set_xticklabels(labels)
    ax4.set_ylabel("overlap area (mm²)")
    ax4.set_title("(d) DCCF @1000 iters, k=0.1: repulsion scale")
    ax4.legend(fontsize=8)
    ax4.grid(True, alpha=0.3, axis="y")
    fig.suptitle("Long-budget regrowth: force balance vs parameter "
                 "(deterministic engine; spread = budget-truncated chaos)")
    fig.tight_layout()
    fig.savefig(dest, dpi=dpi)
    plt.close(fig)
    return dest


def transect_side(model, base_deltas, target_uuid, target_ref, n=25,
                  span=(-3.0, 9.0)):
    """Static probes of U2 translated along the target→U2 axis (mm).

    Positive t separates from the target; negative t deepens. Measured
    per-cause forces on U2, that pair's overlap area, signed edge
    separation s, and the theory wall curve for the pair.
    """
    moved0 = apply_deltas(model, base_deltas)
    polys0 = _posed_polys(moved0)
    names = {fp.uuid: fp.ref for fp in model.footprints}
    c_t = np.array([polys0[names[target_uuid]].centroid.x,
                    polys0[names[target_uuid]].centroid.y])
    c_u = np.array([polys0["U2"].centroid.x, polys0["U2"].centroid.y])
    axis = c_u - c_t
    axis = axis / float(np.linalg.norm(axis))
    halo_mm = BASE_KW["wall_halo_m"] * 1000.0
    rows = []
    for t in np.linspace(span[0], span[1], n):
        deltas = dict(base_deltas)
        dx0, dy0, da0 = deltas.get(U2, (0.0, 0.0, 0.0))
        deltas[U2] = (dx0 + float(t) * float(axis[0]),
                      dy0 + float(t) * float(axis[1]), da0)
        moved = apply_deltas(model, deltas)
        prop = run_placement(moved, PlacementParams(
            **{**BASE_KW, "max_iterations": 0}))
        comps = prop.force_components.get(U2, {})
        polys = _posed_polys(moved)
        pi, pj = polys[names[target_uuid]], polys["U2"]
        area = pi.intersection(pj).area
        if area > 1e-9:
            s = -_overlap_depth_mm(pi, pj, float(axis[0]), float(axis[1]))
        else:
            s = pi.distance(pj)
        cy = _mag(comps.get("courtyard", (0.0, 0.0)))
        at = _mag(comps.get("attraction", (0.0, 0.0)))
        rp = _mag(comps.get("repulsion", (0.0, 0.0)))
        rows.append({
            "t": float(t), "s": float(s), "area": float(area),
            "f_courtyard": cy, "f_attraction": at, "f_repulsion": rp,
            "net": cy - at,
            "theory": _wall_mag(BASE_KW["wall_k_N"], float(s), halo_mm,
                                BASE_KW["wall_exponent"]),
        })
    return rows


def _side_panel(ax, rows, side, pair):
    ts = [r["t"] for r in rows]
    overlap = [r["area"] > 1.0 for r in rows]
    if any(overlap):
        i0 = min(i for i, o in enumerate(overlap) if o)
        i1 = max(i for i, o in enumerate(overlap) if o)
        ax.axvspan(ts[i0], ts[i1], color="C3", alpha=0.12, label="overlap >1mm²")
    ax.plot(ts, [r["f_courtyard"] for r in rows], "o-", ms=3,
            label="measured courtyard on U2 (all neighbors)")
    ax.plot(ts, [r["theory"] for r in rows], "--", label=f"theory wall law ({pair})")
    ax.plot(ts, [r["f_attraction"] for r in rows], "s-", ms=3,
            label="measured attraction on U2")
    ax.set_xlabel(f"U2 offset toward/away {side} (mm, + separates)")
    ax.set_ylabel("force magnitude (N, log)")
    ax.set_yscale("log")
    ax.set_title(f"U2 vs {side}: forces vs geometry (static probes)")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(True, alpha=0.3, which="both")
    ins = inset_axes(ax, width="42%", height="38%", loc="lower right",
                     borderpad=2)
    ins.plot(ts, [r["net"] for r in rows], ".-", ms=3, color="C0")
    ins.axhline(0.0, color="k", linestyle=":", alpha=0.7)
    ins.set_xlabel("offset (mm)", fontsize=7)
    ins.set_ylabel("net (wall−attr, N)", fontsize=7)
    ins.set_title("linear: zero = equilibrium", fontsize=8)
    ins.tick_params(labelsize=7)
    ins.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    ax2.plot(ts, [r["area"] for r in rows], ".--", ms=3, color="C3", alpha=0.7)
    ax2.axhline(1.0, color="k", linestyle=":", alpha=0.5)
    ax2.set_ylabel(f"{pair} area (mm²)", color="C3", fontsize=8)
    return ax


def plot_transect(rows_r11, rows_rv1, dest, dpi):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    _side_panel(ax1, rows_r11, "R11", "R11/U2")
    _side_panel(ax2, rows_rv1, "locked RV1", "RV1/U2")
    rep_max = max([r.get("f_repulsion", 0.0) for r in rows_r11 + rows_rv1] + [0.0])
    fig.suptitle("U2 is wedged: both sides ramp the wall while attraction pulls "
                 "flat between them (halo 4mm, k=0.1)",
                 fontsize=11)
    fig.text(0.5, 0.01,
             f"repulsion on U2 ≤ {rep_max:.1e} N throughout (off-scale, not plotted); "
             "shading = pair overlap >1mm²; inset zero = equilibrium.",
             ha="center", fontsize=8, style="italic")
    fig.subplots_adjust(wspace=0.3, top=0.85, bottom=0.12)
    fig.savefig(dest, dpi=dpi)
    plt.close(fig)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--outdir", default="docs/figures")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--only", nargs="*", choices=FIGURES, default=list(FIGURES))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--save-cells", default=None,
                    help="write sweep cells + transect base deltas to JSON (no plot change)")
    ap.add_argument("--load-cells", default=None,
                    help="replot from a saved cells JSON (skips engine runs)")
    args = ap.parse_args(argv)
    if args.list:
        print(" ".join(FIGURES))
        return 0
    outdir = REPO_ROOT / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    model = _dccf()
    cache = None
    if args.load_cells:
        import json
        cache = json.loads(pathlib.Path(args.load_cells).read_text())
        cache["base_deltas"] = {k: tuple(v)
                                for k, v in cache["base_deltas"].items()}
    if "sweep" in args.only:
        if cache is not None:
            ks_cells, chaos = cache["ks_cells"], [tuple(c) for c in cache["chaos"]]
            halo_cells, rep_cells = cache["halo_cells"], cache["rep_cells"]
        else:
            ks = [round(0.05 + 0.01 * i, 2) for i in range(12)]
            ks_cells = [run_cell(model, f"k={k:.2f}", wall_k_N=k) for k in ks]
            chaos = []
            for k in (0.08, 0.10, 0.12):
                areas = []
                for it in (900, 1100):
                    c = run_cell(model, f"k={k:.2f} x{it}",
                                 wall_k_N=k, max_iterations=it)
                    areas.append(c["total_area"])
                base = next(c["total_area"] for c in ks_cells if c["k"] == k)
                chaos.append((k, min(areas + [base]), max(areas + [base])))
            halo_cells = [run_cell(model, f"halo={h:g}mm",
                                   wall_halo_m=h / 1000.0)
                          for h in (2, 3, 4, 5, 6, 8)]
            rep_cells = [run_cell(model, f"rep={r:g}", repulsion_k_N_m2=r)
                         for r in (1e-15, 2e-9, 2e-8)]
        dest = plot_sweep(ks_cells, chaos, halo_cells, rep_cells,
                          outdir / "fig_regrow_wall_sweep.png", args.dpi)
        print(f"wrote {dest.relative_to(REPO_ROOT)}")
    if "balance" in args.only:
        if cache is not None:
            base_deltas = cache["base_deltas"]
        else:
            prop = run_placement(model, PlacementParams(**BASE_KW))
            base_deltas = prop.deltas
            if args.save_cells and "sweep" not in args.only:
                import json
                pathlib.Path(args.save_cells).write_text(json.dumps(
                    {"base_deltas": {k: list(v)
                                     for k, v in base_deltas.items()}}, indent=1))
        rows_r11 = transect_side(model, base_deltas, R11, "R11")
        rows_rv1 = transect_side(model, base_deltas, RV1, "RV1")
        dest = plot_transect(rows_r11, rows_rv1,
                             outdir / "fig_r11u2_force_balance.png", args.dpi)
        print(f"wrote {dest.relative_to(REPO_ROOT)}")
    if args.save_cells and "sweep" in args.only:
        import json
        pathlib.Path(args.save_cells).write_text(json.dumps(
            {"ks_cells": ks_cells,
             "chaos": [list(c) for c in chaos],
             "halo_cells": halo_cells, "rep_cells": rep_cells,
             "base_deltas": {k: list(v)
                             for k, v in run_placement(
                                 model, PlacementParams(**BASE_KW)).deltas.items()}},
            indent=1))
        print(f"wrote {args.save_cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
