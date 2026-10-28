#!/usr/bin/env python3
"""Plan-view PNG of a footprint-pair overlap (collision truth, not the viewer).

Draws the two bodies exactly as placement collision/legality sees them
(``_footprint_courtyard_polygon``: true outline, pad-copper hull fallback,
or gated debris), plus through-hole pin copper (which exists on every
copper layer), and hatches their intersection — the area legality would
flag for a cross-layer pin keep-out.

Usage:
    python scripts/render_overlap.py /tmp/beads-2m.kicad_pcb R7 IC11
    python scripts/render_overlap.py board.kicad_pcb D2 R3 --out /tmp/d2_r3.png --dpi 200

Output defaults to ``docs/figures/overlap_<A>_<B>.png``.
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
from matplotlib.patches import Polygon as MplPolygon

from kicad_autorouter.board_model import board_model
from kicad_autorouter.io import parse_file
from kicad_autorouter.placement.contact import (
    _footprint_courtyard_polygon,
    pad_copper_polygon,
)

LAYER_COLOR = {"F.Cu": "#e74c3c", "B.Cu": "#2ecc71"}


def _ring(poly):
    return list(poly.exterior.coords)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", help=".kicad_pcb file")
    ap.add_argument("ref_a", help="first footprint ref")
    ap.add_argument("ref_b", help="second footprint ref")
    ap.add_argument("--out", default=None, help="output PNG path")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--margin", type=float, default=5.0,
                    help="mm of context around the pair bbox")
    args = ap.parse_args()

    model = board_model(parse_file(args.board))
    by_ref = {f.ref: f for f in model.footprints}
    for ref in (args.ref_a, args.ref_b):
        if ref not in by_ref:
            print(f"unknown ref {ref!r}", file=sys.stderr)
            return 2
    fa, fb = by_ref[args.ref_a], by_ref[args.ref_b]

    pa = _footprint_courtyard_polygon(fa)
    pb = _footprint_courtyard_polygon(fb)
    pins_a = [pad_copper_polygon(p) for p in fa.pads if p.is_through_hole]
    pins_b = [pad_copper_polygon(p) for p in fb.pads if p.is_through_hole]

    # Cross-layer pin keep-out, same as legality: THT copper of each part
    # (present on every copper layer) against the other's body.
    hits = []
    for pins, other, on_layer in ((pins_a, pb, fb.layer), (pins_b, pa, fa.layer)):
        for pin in pins:
            try:
                inter = pin.intersection(other)
            except Exception:
                continue
            if not inter.is_empty and inter.area > 1e-9:
                hits.append(inter)

    fig, ax = plt.subplots(figsize=(8, 8))
    for fp, poly in ((fa, pa), (fb, pb)):
        ax.add_patch(MplPolygon(
            _ring(poly), closed=True, fill=False,
            edgecolor=LAYER_COLOR.get(fp.layer, "#9fb0c0"),
            linewidth=1.5, linestyle="-" if fp.layer == "F.Cu" else "--",
            label=f"{fp.ref} body {fp.layer} ({poly.area:.1f} mm²)"))
    for pin in pins_a:
        ax.add_patch(MplPolygon(_ring(pin), closed=True, fill=True,
                                facecolor="#f5a623", edgecolor="none", alpha=0.6))
    for pin in pins_b:
        ax.add_patch(MplPolygon(_ring(pin), closed=True, fill=True,
                                facecolor="#f5a623", edgecolor="none", alpha=0.6,
                                hatch="///"))
    total = 0.0
    for i, hit in enumerate(hits):
        total += hit.area
        geoms = list(getattr(hit, "geoms", [hit]))
        for g in geoms:
            if g.is_empty or not hasattr(g, "exterior"):
                continue
            ax.add_patch(MplPolygon(_ring(g), closed=True, fill=True,
                                    facecolor="#e06c75", edgecolor="#e06c75",
                                    alpha=0.75,
                                    label="pin∩courtyard" if i == 0 else None))
    ax.set_aspect("equal")
    ax.invert_yaxis()  # KiCad board coords: +y points down, like the viewer.
    bounds = pa.bounds + pb.bounds
    ax.set_xlim(min(bounds[0], bounds[4]) - args.margin,
                max(bounds[2], bounds[6]) + args.margin)
    ax.set_ylim(min(bounds[1], bounds[5]) - args.margin,
                max(bounds[3], bounds[7]) + args.margin)
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_title(f"{fa.ref} ({fa.layer}) vs {fb.ref} ({fb.layer})  "
                 f"pin overlap {total:.2f} mm²")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()

    out = (pathlib.Path(args.out) if args.out else
           REPO_ROOT / "docs" / "figures" / f"overlap_{fa.ref}_{fb.ref}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=args.dpi)
    print(f"wrote {out} ({total:.2f} mm² pin overlap)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
