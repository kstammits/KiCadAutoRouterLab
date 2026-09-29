#!/usr/bin/env python3
"""Headless first pass: load a .kicad_pcb, nudge unlocked parts + add one track, save.

Run under KiCad's bundled Python (the only place with the `pcbnew` bindings):

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 \
        scripts/run_autoroute.py [input.kicad_pcb] [-o output.kicad_pcb]

This is a baby step: it proves we can import a board, make a slight update to
component placement and traces, and resave it. No routing algorithm yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the src/ package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter import pcbnew_adapter as adapter  # noqa: E402


def main(argv=None) -> int:
    repo = Path(__file__).resolve().parent.parent
    default_in = repo / "tests" / "fixtures" / "minimal.kicad_pcb"

    p = argparse.ArgumentParser(description="Load, slightly update, and resave a KiCad board.")
    p.add_argument("input", nargs="?", default=str(default_in), help=".kicad_pcb to load")
    p.add_argument("-o", "--output", default=None, help="where to save the updated board")
    p.add_argument("--dx-mm", type=float, default=1.0, help="x nudge for unlocked parts (mm)")
    p.add_argument("--dy-mm", type=float, default=0.0, help="y nudge for unlocked parts (mm)")
    p.add_argument("--no-track", action="store_true", help="skip adding the demo track")
    args = p.parse_args(argv)

    in_path = Path(args.input)
    out_path = (
        Path(args.output)
        if args.output
        else in_path.with_name(in_path.stem + "_updated.kicad_pcb")
    )

    board = adapter.load_board(in_path)
    print(f"loaded {in_path}: {len(list(board.GetFootprints()))} footprints")

    moved = adapter.nudge_unlocked_footprints(board, dx_mm=args.dx_mm, dy_mm=args.dy_mm)
    if moved:
        for ref, old, new in moved:
            print(
                f"  nudged {ref}: ({old[0]:.2f},{old[1]:.2f}) -> "
                f"({new[0]:.2f},{new[1]:.2f}) mm"
            )
    else:
        print("  no unlocked footprints to nudge")

    if not args.no_track:
        # A short demo segment in the open middle of the board (clears keepout zones).
        x0, y0, x1, y1 = 80.0, 95.0, 135.0, 95.0
        w = 0.25
        track = adapter.add_track(board, x0, y0, x1, y1, width_mm=w)
        print(f"  added track on {track.GetLayerName()}: ({x0},{y0})->({x1},{y1}) mm, w={w}mm")

    saved = adapter.save_board(board, out_path)
    print(f"saved -> {saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
