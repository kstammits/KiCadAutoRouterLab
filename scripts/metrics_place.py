#!/usr/bin/env python3
"""Decap-gap + silk-association metrics across pipeline variants.

Usage: .venv/bin/python scripts/metrics_place.py <board> [board...]
Pure read-only; prints legality pairs, U3-family gaps, global cap gaps,
and label-offset stats per board.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import board_model  # noqa: E402
from kicad_autorouter.placement.legality import check_legality  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402

# Window-LED guidance targets (mirrors place_strip LED_GHOSTS anchors).
LED_REFS = ("D1", "D2", "D3", "D4", "D5")
LED_ANCHORS = {
    "D5": (78.0, 38.0), "D4": (84.0, 38.0), "D1": (90.0, 38.0),
    "D3": (96.0, 38.0), "D2": (102.0, 38.0),
}


def main(paths):
    for path in paths:
        m = board_model(parse_file(Path(path)))
        refs = {fp.uuid: (fp.ref or "?") for fp in m.footprints}
        print(f"=== {path}: {len(m.footprints)} fps")
        rep = check_legality(m, {})
        print(f"  legality: pairs={len(rep.overlapping_pairs)} "
              f"max={rep.max_pair_overlap_mm2:.3f} wall={rep.max_wall_penetration_mm:.3f}")
        for a, b, ar in rep.overlapping_pairs:
            print(f"    {refs.get(a, a[:8])}-{refs.get(b, b[:8])} {ar:.3f}")

        # Decap gaps: U3 family + global 2-terminal caps to nearest U*.
        chips = {fp.ref: fp for fp in m.footprints
                 if fp.ref and fp.ref.startswith("U")}
        u3 = chips.get("U3")
        fam = {}
        worst, worst_ref, total, n = 0.0, "", 0.0, 0
        for fp in m.footprints:
            if fp.ref and fp.ref.startswith("C") and len(fp.pads) <= 2 and chips:
                d = min(math.hypot(fp.x_mm - c.x_mm, fp.y_mm - c.y_mm)
                        for c in chips.values())
                total += d
                n += 1
                if d > worst:
                    worst, worst_ref = d, fp.ref
                if u3 is not None and fp.ref in (
                        "C10", "C11", "C12", "C13", "C15"):
                    fam[fp.ref] = math.hypot(fp.x_mm - u3.x_mm, fp.y_mm - u3.y_mm)
        print(f"  U3 family gaps: " +
              " ".join(f"{k}={v:.2f}" for k, v in sorted(fam.items())))
        print(f"  global cap->nearest-U: mean={total / max(n, 1):.2f} "
              f"max={worst:.2f} ({worst_ref}) n={n}")

        # Silk association: label local offset vs parent courtyard size.
        offs = []
        for fp in m.footprints:
            if fp.label is None:
                continue
            d = math.hypot(fp.label.local_x_mm, fp.label.local_y_mm)
            hw = 0.0
            if fp.courtyard:
                xs = [p.x_mm for seg in fp.courtyard for p in seg]
                ys = [p.y_mm for seg in fp.courtyard for p in seg]
                hw = max(max(xs) - min(xs), max(ys) - min(ys)) / 2.0
            offs.append((d, d / max(hw, 1e-9), fp.ref or "?", hw))
        offs.sort(reverse=True)
        print(f"  labels: {len(offs)} with offset "
              f"mean={sum(o[0] for o in offs) / max(len(offs), 1):.2f}mm "
              f"max={offs[0][0]:.2f}mm ({offs[0][2]}, hw={offs[0][3]:.2f})")
        for d, ratio, ref, hw in offs[:5]:
            print(f"    {ref}: offset={d:.2f}mm hw={hw:.2f} ratio={ratio:.2f}")

        # Window LEDs: position vs band (y 30-45) and anchor distance.
        by_ref = {fp.ref: fp for fp in m.footprints}
        for ref in LED_REFS:
            fp = by_ref.get(ref)
            if fp is None:
                continue
            ax, ay = LED_ANCHORS[ref]
            ad = math.hypot(fp.x_mm - ax, fp.y_mm - ay)
            print(f"  LED {ref}: pos=({fp.x_mm:.1f},{fp.y_mm:.1f}) "
                  f"anchor_d={ad:.1f}")


if __name__ == "__main__":
    main(sys.argv[1:])
