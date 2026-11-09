"""Headless label-orbit run + renders for visual inspection.

1. Loads tube111, runs placement (orbit always on, single spring tether).
2. Dual-writes like the server (commit_placement + per-uuid tree nudge
   with label deltas) and saves the placed .kicad_pcb.
3. Renders the app SVG before (filed) and after (proposal preview).
All outputs go to OUT_DIR (caller picks /tmp or scratch).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from kicad_autorouter.board_model import apply_deltas, board_model  # noqa: E402
from kicad_autorouter.io import nudge_footprint_by_uuid  # noqa: E402
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402
from kicad_autorouter.sexpr import parse_file, to_sexpr  # noqa: E402
from kicad_autorouter.svg_render import render_board_svg  # noqa: E402


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "scratch"
    out_dir.mkdir(parents=True, exist_ok=True)
    iters = int(sys.argv[2]) if len(sys.argv) > 2 else 150

    pcb_path = REPO / "tests" / "fixtures" / "tube111.kicad_pcb"
    tree = parse_file(pcb_path)
    model = board_model(tree)
    print(f"parts={len(model.footprints)} labeled={sum(1 for f in model.footprints if f.label is not None)}")

    params = PlacementParams(
        max_iterations=iters,
        label_clearance_m=0.005,
    )
    prop = run_placement(model, params)
    print(f"stop={prop.stop_reason} iters={prop.iterations} "
          f"moved={len(prop.deltas)} labels_moved={len(prop.label_deltas)}")

    before_svg = out_dir / "labels_before.svg"
    before_svg.write_text(render_board_svg(model, title="tube111 filed"), encoding="utf-8")

    preview = apply_deltas(
        model, dict(prop.deltas),
        label_deltas=dict(prop.label_deltas) or None,
    )
    after_svg = out_dir / "labels_after.svg"
    after_svg.write_text(
        render_board_svg(preview, title="tube111 orbit preview"), encoding="utf-8")

    # Server-style dual writeback into a placed file.
    placed_tree = tree
    for uuid, (dx, dy, da) in prop.deltas.items():
        ldx, ldy = (prop.label_deltas or {}).get(uuid, (0.0, 0.0))
        if dx != 0.0 or dy != 0.0 or da != 0.0 or (ldx, ldy) != (0.0, 0.0):
            placed_tree = nudge_footprint_by_uuid(
                placed_tree, uuid, dx, dy, da,
                label_dx_mm=ldx, label_dy_mm=ldy)
    placed_pcb = out_dir / "tube111_orbit_placed.kicad_pcb"
    placed_pcb.write_text(to_sexpr(placed_tree) + "\n", encoding="utf-8")
    print(f"wrote {before_svg} {after_svg} {placed_pcb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
