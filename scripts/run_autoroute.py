#!/usr/bin/env python3
"""Headless placement pass: parse .kicad_pcb -> force-spring proposal -> UUID writeback -> save.

Runs under the project .venv (pure Python; no `pcbnew` needed):

    .venv/bin/python scripts/run_autoroute.py [input.kicad_pcb] [-o output.kicad_pcb]

Locked footprints never move; deltas are applied per footprint UUID via
``io.nudge_footprint_by_uuid``. With --validate the saved board is DRC-checked
via kicad-cli: a load failure means the writeback broke the file (exit 1);
design-rule violations are reported but do not fail the run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import board_model  # noqa: E402
from kicad_autorouter.io import nudge_footprint_by_uuid  # noqa: E402
from kicad_autorouter.placement import PlacementParams, run_placement  # noqa: E402
from kicad_autorouter.sexpr import parse_file, to_sexpr  # noqa: E402
from kicad_autorouter.validate import KicadCliNotFound, validate_pcb  # noqa: E402

PARAM_FIELDS = (
    "repulsion_kr",
    "attraction_ka",
    "ideal_length_mm",
    "max_iterations",
    "convergence_eps_mm",
    "demo_jitter_mm",
)


def main(argv=None) -> int:
    repo = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description="Force-spring placement pass for a .kicad_pcb.")
    p.add_argument(
        "input", nargs="?", default=str(repo / "tests" / "fixtures" / "minimal.kicad_pcb")
    )
    p.add_argument("-o", "--output", help="where to save the placed board")
    for name in PARAM_FIELDS:
        p.add_argument(
            f"--{name.replace('_', '-')}",
            type=int if name == "max_iterations" else float,
            dest=name,
            default=None,
        )
    p.add_argument("--validate", action="store_true", help="DRC-check the saved board")
    args = p.parse_args(argv)

    in_path = Path(args.input)
    out_path = (
        Path(args.output)
        if args.output
        else in_path.with_name(in_path.stem + "_placed.kicad_pcb")
    )

    try:
        tree = parse_file(in_path)
        if tree.head != "kicad_pcb":
            raise ValueError(f"expected (kicad_pcb ...) in {in_path}, got head {tree.head!r}")
        model = board_model(tree)
        params = PlacementParams.from_dict(
            {n: getattr(args, n) for n in PARAM_FIELDS if getattr(args, n) is not None}
        )

        proposal = run_placement(model, params)
        placed = tree
        for uuid_, (dx, dy) in proposal.deltas.items():
            placed = nudge_footprint_by_uuid(placed, uuid_, dx, dy)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(to_sexpr(placed) + "\n", encoding="utf-8")

        by_uuid = {fp.uuid: fp for fp in model.footprints if fp.uuid}
        locked = sum(fp.locked for fp in model.footprints)
        print(f"loaded {in_path}: {len(model.footprints)} footprints ({locked} locked)")
        print(
            f"placement: moved={len(proposal.deltas)} iterations={proposal.iterations} "
            f"max_disp={proposal.final_max_disp_mm:.3f}mm elapsed={proposal.elapsed_s:.2f}s"
        )
        for uuid_, (dx, dy) in proposal.deltas.items():
            fp = by_uuid[uuid_]
            label = fp.ref or uuid_[:8]
            print(
                f"  {label}: ({fp.x_mm:.2f},{fp.y_mm:.2f}) -> "
                f"({fp.x_mm + dx:.2f},{fp.y_mm + dy:.2f}) mm"
            )

        if args.validate:
            try:
                report = validate_pcb(out_path)
            except KicadCliNotFound as exc:
                print(f"warning: {exc}; skipping DRC", file=sys.stderr)
                return 0
            if not report.loaded:
                print(f"DRC: failed to load {out_path}: {report.error}", file=sys.stderr)
                return 1
            print(f"DRC: loaded OK, {len(report.violations)} violation(s)")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"saved -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
