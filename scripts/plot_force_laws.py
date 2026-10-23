#!/usr/bin/env python3
"""Parameter-response and live placement figures for the physics primer.

Generates the figures embedded in `docs/physics_primer.qmd`. Analytic curves
use the normative formulas in `docs/si_force_laws.md` and live
`PlacementParams` defaults so the plots cannot drift from the engine. The
ghost pull-in and overlap heatmap run the real placement solver on synthetic
boards.

Usage:
    python scripts/plot_force_laws.py [--outdir docs/figures] [--dpi 150]
    python scripts/plot_force_laws.py --list
    python scripts/plot_force_laws.py --only wall ghost

Figures (all PNG, committed under `docs/figures/`):
    fig_repulsion_vs_distance.png  Coulomb k/d^2 vs constant attraction + equilibrium
    fig_attraction_fanout.png       signal / power /(fanout-1) / decap-boost weighting
    fig_wall_profile.png            unified wall law F=k*(1-s/h)^p, halo variants
    fig_wall_exponent.png           wall-law exponent F=k*(1-s/h)^p comparison
    fig_ghost_pullin.png            ghost pull-in vs per-spring weight ka
    fig_overlap_heatmap.png         overlap resolution vs wall_k_N and damping
    fig_damping_decay.png           implicit-Euler velocity decay for several damping values
    fig_mass_acceleration.png       a=F/m sanity anchor vs courtyard area
    fig_integrator_stability.png    per-step drift vs dt for typical forces + vmax ceiling
"""

from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from kicad_autorouter.board_model import (
    BoardModel,
    Footprint,
    Pad,
    Point,
    apply_deltas,
)
from kicad_autorouter.ghost import make_ghost_attractor
from kicad_autorouter.placement import (
    PlacementParams,
    _collect_pad_nodes,
    _footprint_courtyard_polygon,
    _wall_mag,
    run_placement,
)


def _defaults() -> PlacementParams:
    return PlacementParams()


def _save_optimized(fig, dest: pathlib.Path, dpi: int) -> None:
    """Save a figure, then recompress losslessly in place (Pillow).

    Strips a fully-opaque alpha channel (pixel-identical when unused, which
    is the case for all figures here) and re-saves with ``optimize=True``.
    Silent no-op fallback to the plain savefig output when Pillow is missing
    or anything goes wrong — never fail a regen over compression.
    """
    fig.savefig(dest, dpi=dpi)
    try:
        from PIL import Image
        im = Image.open(dest)
        if im.mode == "RGBA":
            lo, hi = im.getchannel("A").getextrema()
            if (lo, hi) == (255, 255):
                im = im.convert("RGB")
        im.save(dest, optimize=True)
    except Exception:
        pass


def fig_repulsion_vs_distance(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    d_mm = np.logspace(-2, 2, 400)  # 0.01 mm .. 100 mm
    d_m = d_mm * 1e-3
    k_rep_default = p.repulsion_k_N_m2
    k_attr = p.attraction_k_N
    fig, ax = plt.subplots(figsize=(10, 5))
    for k_rep in (1e-9, k_rep_default, 5e-9, 1e-8):
        ax.loglog(d_mm, k_rep / d_m**2, label=f"$k_{{rep}}$ = {k_rep:.0e} N·m²")
    ax.loglog(d_mm, np.full_like(d_mm, k_attr), "k--", label=f"$k_{{attr}}$ = {k_attr:.0e} N")
    d_eq = np.sqrt(k_rep_default / k_attr) * 1000
    ax.axvline(d_eq, color="gray", linestyle=":", alpha=0.7,
               label=f"Equilibrium (default) = {d_eq:.2f} mm")
    ax.set_xlabel("Pad separation (mm)")
    ax.set_ylabel("Force magnitude (N)")
    ax.set_title("Pad Repulsion ($k_{rep}/d^2$) vs. Constant Attraction")
    ax.legend()
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    dest = out / "fig_repulsion_vs_distance.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    return dest


def fig_attraction_fanout(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    k_attr = p.attraction_k_N
    fanouts = np.arange(2, 21)
    f_signal = np.full_like(fanouts, k_attr, dtype=float) * 1000  # mN
    f_power = k_attr * p.power_attraction_scale / (fanouts - 1) * 1000
    f_decap = f_power * p.decap_boost
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(fanouts, f_signal, "o-", label="Signal (1.0)", color="#a3e635")
    ax.plot(fanouts, f_power, "s-", label="Power (1/(fanout-1))", color="#f59e0b")
    ax.plot(fanouts, f_decap, "^-", label=f"Decap power edge (×{p.decap_boost:g})", color="#2dd4bf")
    ax.set_xlabel("Net fanout (number of pads on net)")
    ax.set_ylabel("Attraction force per edge (mN)")
    ax.set_title("Net Attraction Weighting by Fanout and Net Type")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_yscale("log")
    fig.tight_layout()
    dest = out / "fig_attraction_fanout.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    return dest


def fig_wall_profile(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    k_wall = p.wall_k_N
    exponent = p.wall_exponent
    halo_default_mm = p.wall_halo_m * 1000.0
    s_mm = np.linspace(halo_default_mm, -halo_default_mm, 400)
    fig, ax = plt.subplots(figsize=(10, 5))
    for halo_mm in (3.0, halo_default_mm, 10.0):
        # Evaluate each halo on its own reach, resampled onto the default axis
        # via the shared law F = k*(1-s/h)^p; clip at zero past reach.
        f = np.array([_wall_mag(k_wall, s, halo_mm, exponent) for s in s_mm])
        lw = 2.5 if halo_mm == halo_default_mm else 1.2
        alpha = 1.0 if halo_mm == halo_default_mm else 0.55
        ax.plot(s_mm, f * 1000, linewidth=lw, alpha=alpha,
                label=f"halo = {halo_mm:g} mm" + (" (default)" if halo_mm == halo_default_mm else ""))
    ax.axvline(0, color="k", linestyle=":", alpha=0.5, label="contact (s=0)")
    ax.axhline(k_wall * 1000, color="k", linestyle=":", alpha=0.5,
               label=f"k_wall = {k_wall * 1000:.0f} mN")
    ax.set_xlabel("Signed separation s (mm) [gap +  ←→  - penetration]")
    ax.set_ylabel("Force magnitude (mN)")
    ax.set_title(f"Courtyard Force Profile: F = k·(1−s/h)^p (p={exponent:g})")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    dest = out / "fig_wall_profile.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    return dest


def fig_damping_decay(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    """Implicit-Euler decay v_n = v0/(1+c*dt/m)^n for an 0805 (~8 mg)."""
    m_kg = 8e-6
    dt = p.dt_s
    steps = np.arange(0, 51)
    v0 = 0.1  # m/s initial kick
    fig, ax = plt.subplots(figsize=(10, 5))
    for c in (0.05, p.damping_Ns_m, 0.3):
        decay = 1.0 / (1.0 + c * dt / m_kg)
        ax.semilogy(steps, v0 * decay**steps, "o-" if c == p.damping_Ns_m else "s-",
                    markevery=5, linewidth=2 if c == p.damping_Ns_m else 1.2,
                    label=f"c = {c:g} N·s/m" + (" (default)" if c == p.damping_Ns_m else ""))
    ax.set_xlabel(f"Steps (dt = {dt * 1000:g} ms)")
    ax.set_ylabel("Velocity (m/s, log scale)")
    ax.set_title(f"Viscous Damping Decay (0805, m ≈ 8 mg; stable for any dt)")
    ax.legend()
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    dest = out / "fig_damping_decay.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    return dest


def fig_mass_acceleration(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    """a = F/m sanity anchor: acceleration vs courtyard area for typical forces."""
    area_mm2 = np.logspace(np.log10(0.5), 2, 200)
    area_m2 = area_mm2 * 1e-6
    m = p.mass_density_kg_m2 * area_m2 + p.mass_floor_kg
    forces = {
        f"ghost ({p.ghost_k_N * 1e6:.0f} µN)": p.ghost_k_N,
        f"attraction ({p.attraction_k_N * 1e3:.0f} mN)": p.attraction_k_N,
        f"wall ({p.wall_k_N * 1e3:.0f} mN)": p.wall_k_N,
    }
    fig, ax = plt.subplots(figsize=(10, 5))
    for label, f in forces.items():
        ax.loglog(area_mm2, f / m, label=f"a, F = {label}")
    # 0805 anchor: 2.5 mm² courtyard, ~8 mg
    m_0805 = p.mass_density_kg_m2 * 2.5e-6 + p.mass_floor_kg
    ax.axvline(2.5, color="gray", linestyle=":", alpha=0.7, label="0805 (~2.5 mm²)")
    ax.set_xlabel("Courtyard area (mm²)")
    ax.set_ylabel("Acceleration (m/s²)")
    ax.set_title("Mass Sanity Anchor: a = F/m (heavier parts accelerate slower)")
    ax.legend()
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    dest = out / "fig_mass_acceleration.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    # Silence unused-variable lint for the anchor mass (kept for documentation).
    _ = m_0805
    return dest


def fig_wall_exponent(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    """Wall-law exponent comparison: F = k*(1-s/h)^p for p in {1, 1.5, 2, 3}.

    p > 1 softens the far-halo approach (gentler near-miss) and stiffens
    deep penetration; all curves meet at k_wall at contact (s=0).
    """
    k_wall = p.wall_k_N
    halo_mm = p.wall_halo_m * 1000.0
    s_mm = np.linspace(halo_mm, -halo_mm, 400)
    fig, ax = plt.subplots(figsize=(10, 5))
    for exp in (1.0, 1.5, 2.0, 3.0):
        f = np.array([_wall_mag(k_wall, s, halo_mm, exp) for s in s_mm])
        lw = 2.5 if exp == 1.0 else 1.4
        ax.plot(s_mm, f * 1000, linewidth=lw,
                label=f"p = {exp:g}" + (" (default)" if exp == 1.0 else ""))
    ax.axvline(0, color="k", linestyle=":", alpha=0.5, label="contact (s=0)")
    ax.set_xlabel("Signed separation s (mm) [gap +  ←→  - penetration]")
    ax.set_ylabel("Force magnitude (mN)")
    ax.set_title(f"Wall-Law Exponent: F = k·(1−s/h)^p (k={k_wall * 1000:.0f} mN, h={halo_mm:.0f} mm)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    dest = out / "fig_wall_exponent.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    return dest


def _overlap_pair(dx: float = 1.0) -> BoardModel:
    """Two 6x4 mm parts with ``dx`` mm centre spacing, board-space courtyards."""
    def fp(ref: str, uuid: str, x: float) -> Footprint:
        return Footprint(
            ref=ref, footprint_id="test:R", layer="F.Cu",
            x_mm=x, y_mm=0.0, angle_deg=0.0,
            pads=(
                Pad("1", f"N-{ref}-1", Point(x - 1.0, 0.0), (1.0, 1.0),
                    "circle", 0.0, "smd", 0.0, ("F.Cu",)),
                Pad("2", f"N-{ref}-2", Point(x + 1.0, 0.0), (1.0, 1.0),
                    "circle", 0.0, "smd", 0.0, ("F.Cu",)),
            ),
            courtyard=(
                (Point(x - 3, -2), Point(x + 3, -2)),
                (Point(x + 3, -2), Point(x + 3, 2)),
                (Point(x + 3, 2), Point(x - 3, 2)),
                (Point(x - 3, 2), Point(x - 3, -2)),
            ),
            locked=False, uuid=uuid, ghost=False, ghost_attractions=(),
        )

    a, b = fp("A", "uuid-a", 0.0), fp("B", "uuid-b", dx)
    edge = 30.0
    return BoardModel(
        footprints=(a, b), keepout_zones=(), nets={},
        by_ref={"A": a, "B": b},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={"uuid-a": 0, "uuid-b": 0},
    )


def _pair_overlap(model: BoardModel, deltas: dict) -> float:
    moved = apply_deltas(model, deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(pad.position.x_mm, pad.position.y_mm) for pad in pads])
    polys = []
    for uuid in ("uuid-a", "uuid-b"):
        fp = next(f for f in moved.footprints if f.uuid == uuid)
        polys.append(_footprint_courtyard_polygon(
            fp, pad_positions=pos, pad_indices=uuid_to_idx.get(uuid, [])))
    if polys[0].is_empty or polys[1].is_empty:
        return 0.0
    return polys[0].intersection(polys[1]).area


def fig_ghost_pullin(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    """Pull-in after 500 iterations vs. the per-spring ghost weight ``ka``."""
    def model_for(ka: float, dist: float = 20.0) -> BoardModel:
        fp = Footprint(
            ref="A", footprint_id="test:R", layer="F.Cu",
            x_mm=0.0, y_mm=0.0, angle_deg=0.0,
            pads=(
                Pad("1", "N1", Point(-1.0, 0.0), (1.0, 1.0),
                    "circle", 0.0, "smd", 0.0, ("F.Cu",)),
                Pad("2", "N2", Point(1.0, 0.0), (1.0, 1.0),
                    "circle", 0.0, "smd", 0.0, ("F.Cu",)),
            ),
            courtyard=(
                (Point(-3, -2), Point(3, -2)),
                (Point(3, -2), Point(3, 2)),
                (Point(3, 2), Point(-3, 2)),
                (Point(-3, 2), Point(-3, -2)),
            ),
            locked=False, uuid="uuid-a", ghost=False, ghost_attractions=(),
        )
        ghost = make_ghost_attractor(
            "GHOST", dist, 0.0, target_ref="A", ka=ka, uuid="uuid-ghost")
        edge = 60.0
        return BoardModel(
            footprints=(fp,), ghost_footprints=(ghost,),
            keepout_zones=(), nets={}, by_ref={"A": fp},
            edge_cuts=(
                (Point(-edge, -edge), Point(edge, -edge)),
                (Point(edge, -edge), Point(edge, edge)),
                (Point(edge, edge), Point(-edge, edge)),
                (Point(-edge, edge), Point(-edge, -edge)),
            ),
            footprint_region={"uuid-a": 0, "uuid-ghost": 0},
        )

    ka_vals = np.array([0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0])
    pull_in = np.zeros_like(ka_vals)
    for i, ka in enumerate(ka_vals):
        params = PlacementParams(
            max_iterations=500,
            convergence_eps_m=1e-15,
            repulsion_k_N_m2=1e-15,
            attraction_k_N=1e-15,
        )
        prop = run_placement(model_for(float(ka)), params)
        pull_in[i] = prop.deltas.get("uuid-a", (0.0, 0.0, 0.0))[0]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.semilogx(ka_vals, pull_in, "o-", color="#c4b5fd", linewidth=2, markersize=7)
    ax.axvline(0.1, color="gray", linestyle=":", alpha=0.7, label="UI default ka = 0.1")
    ax.axvline(160, color="gray", linestyle="--", alpha=0.7,
               label="attraction-scale ka ≈ 160")
    ax.set_xlabel("Ghost spring weight ka (dimensionless)")
    ax.set_ylabel("Pull-in after 500 iterations (mm)")
    ax.set_title(f"Ghost Pull-In vs. ka (k_ghost = {p.ghost_k_N * 1e6:.0f} µN, anchor 20 mm away)")
    ax.legend()
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    dest = out / "fig_ghost_pullin.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    return dest


def fig_overlap_heatmap(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    """Overlap resolution vs. wall stiffness and damping after 50 iterations."""
    wall_k_vals = np.logspace(-2, -1, 8)
    damping_vals = np.logspace(-1, 0, 8)
    results = np.zeros((len(wall_k_vals), len(damping_vals)))

    for i, wall_k in enumerate(wall_k_vals):
        for j, damping in enumerate(damping_vals):
            model = _overlap_pair(dx=1.0)
            params = PlacementParams(
                max_iterations=50,
                convergence_eps_m=1e-15,
                wall_k_N=float(wall_k),
                damping_Ns_m=float(damping),
                repulsion_k_N_m2=1e-15,
                attraction_k_N=1e-15,
                ghost_k_N=1e-15,
            )
            prop = run_placement(model, params)
            final_overlap = _pair_overlap(model, prop.deltas)
            initial_overlap = _pair_overlap(model, {})
            results[i, j] = (
                1.0 - final_overlap / initial_overlap
                if initial_overlap > 0.0 else 1.0
            )

    fig, ax = plt.subplots(figsize=(10, 5))
    im = ax.imshow(
        results, origin="lower", aspect="auto", cmap="RdYlGn",
        extent=[
            np.log10(damping_vals[0]), np.log10(damping_vals[-1]),
            np.log10(wall_k_vals[0]), np.log10(wall_k_vals[-1]),
        ],
        vmin=0, vmax=1,
    )
    ax.plot(np.log10(p.damping_Ns_m), np.log10(p.wall_k_N), "w*",
            markersize=20,
            label=f"Default ({p.wall_k_N:g}, {p.damping_Ns_m:g})")
    ax.set_xticks(np.log10(damping_vals))
    ax.set_xticklabels([f"{d:.2f}" for d in damping_vals])
    ax.set_yticks(np.log10(wall_k_vals))
    ax.set_yticklabels([f"{w:.2f}" for w in wall_k_vals])
    ax.set_xlabel("damping_Ns_m")
    ax.set_ylabel("wall_k_N")
    ax.set_title("Overlap Resolution Rate (50 iterations, 1 mm initial overlap)")
    fig.colorbar(im, ax=ax, label="Resolution (1 = fully separated)")
    ax.legend()
    fig.tight_layout()
    dest = out / "fig_overlap_heatmap.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    return dest


def fig_integrator_stability(p: PlacementParams, out: pathlib.Path, dpi: int) -> pathlib.Path:
    """Per-step drift vs timestep for typical forces on an 0805 (~8 mg).

    One semi-implicit Euler step from rest: v = (F/m)*dt decayed by
    1/(1+c*dt/m), drift = min(v*dt, vmax*dt). The vmax ceiling binds first
    for wall-scale forces; the dt_min..dt_max band is the adaptive guard's
    operating range. Guidance: dt above the band steps over contacts
    (tunneling risk even with the rigid-step clamp); dt below wastes
    iterations inside the friction deadband.
    """
    m_kg = 8e-6
    c = p.damping_Ns_m
    vmax = p.vmax_m_s
    dt = np.logspace(-4, np.log10(5e-3), 300)
    forces = {
        f"ghost ({p.ghost_k_N * 1e6:.0f} µN)": p.ghost_k_N,
        f"attraction ({p.attraction_k_N * 1e3:.0f} mN)": p.attraction_k_N,
        f"wall ({p.wall_k_N * 1e3:.0f} mN)": p.wall_k_N,
    }
    fig, ax = plt.subplots(figsize=(10, 5))
    for label, f in forces.items():
        v = (f / m_kg) * dt / (1.0 + c * dt / m_kg)
        drift_mm = np.minimum(v * dt, vmax * dt) * 1000.0
        ax.loglog(dt * 1000, drift_mm, label=f"drift/step, F = {label}")
    ax.loglog(dt * 1000, vmax * dt * 1000.0, "k--",
              label=f"vmax ceiling ({vmax:g} m/s × dt)")
    ax.axvspan(p.dt_min_s * 1000, p.dt_max_s * 1000, color="gray", alpha=0.15,
               label=f"guard band ({p.dt_min_s * 1000:g}–{p.dt_max_s * 1000:g} ms)")
    ax.axvline(p.dt_s * 1000, color="k", linestyle=":",
               label=f"default dt = {p.dt_s * 1000:g} ms")
    ax.set_xlabel("Timestep dt (ms)")
    ax.set_ylabel("Drift per step (mm)")
    ax.set_title("Integrator Guidance: One-Step Drift vs dt (0805, from rest)")
    ax.legend(fontsize="small")
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    dest = out / "fig_integrator_stability.png"
    _save_optimized(fig, dest, dpi)
    plt.close(fig)
    return dest


FIGURES = (
    ("repulsion", fig_repulsion_vs_distance),
    ("fanout", fig_attraction_fanout),
    ("wall", fig_wall_profile),
    ("wall_exponent", fig_wall_exponent),
    ("ghost", fig_ghost_pullin),
    ("heatmap", fig_overlap_heatmap),
    ("damping", fig_damping_decay),
    ("mass", fig_mass_acceleration),
    ("integrator", fig_integrator_stability),
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--outdir", default="docs/figures",
                    help="output directory for PNGs (default: docs/figures)")
    ap.add_argument("--dpi", type=int, default=150, help="figure DPI (default: 150)")
    ap.add_argument("--list", action="store_true", help="list figure names and exit")
    ap.add_argument("--only", nargs="*", choices=[n for n, _ in FIGURES], default=None,
                    help="generate only these figures")
    args = ap.parse_args(argv)

    if args.list:
        for name, _ in FIGURES:
            print(name)
        return 0

    out = REPO_ROOT / args.outdir if not pathlib.Path(args.outdir).is_absolute() else pathlib.Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    params = _defaults()
    print(f"PlacementParams defaults: kr={params.repulsion_k_N_m2:g} ka={params.attraction_k_N:g} "
          f"wall={params.wall_k_N:g} halo={params.wall_halo_m:g} "
          f"ghost={params.ghost_k_N:g} c={params.damping_Ns_m:g} dt={params.dt_s:g}")
    made: list[pathlib.Path] = []
    for name, fn in FIGURES:
        if args.only and name not in args.only:
            continue
        dest = fn(params, out, args.dpi)
        print(f"wrote {dest.relative_to(REPO_ROOT)} ({dest.stat().st_size // 1024} KiB)")
        made.append(dest)
    if not made:
        print("nothing selected")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
