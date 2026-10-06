#!/usr/bin/env python3
"""Headless placement pass: parse .kicad_pcb -> force-spring proposal -> UUID writeback -> save.

Runs under the project .venv (pure Python; no `pcbnew` needed):

    .venv/bin/python scripts/run_autoroute.py [input.kicad_pcb] [-o output.kicad_pcb]

Locked footprints never move; deltas are applied per footprint UUID via
``io.nudge_footprint_by_uuid``. With --validate the saved board is DRC-checked
via kicad-cli: a load failure means the writeback broke the file (exit 1);
design-rule violations are reported but do not fail the run.

Iterative mode: use --step-iterations N --max-total-iterations M to run multiple
placement steps, accepting each step's proposal before the next (like the UI
"Step placement" / "Accept proposal" flow).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.board_model import apply_deltas, board_model  # noqa: E402
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
    "stub",
)


def main(argv=None) -> int:
    repo = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description="Force-spring placement pass for a .kicad_pcb.")
    p.add_argument(
        "input", nargs="?", default=str(repo / "tests" / "fixtures" / "minimal.kicad_pcb")
    )
    p.add_argument("-o", "--output", help="where to save the placed board")
    for name in PARAM_FIELDS:
        if name == "stub":
            g = p.add_mutually_exclusive_group()
            g.add_argument("--stub", dest="stub", action="store_true", default=False,
                           help="use stub mode (identity deltas, no physics)")
            g.add_argument("--no-stub", dest="stub", action="store_false",
                           help="run vectorized numpy force-spring simulation (default)")
        else:
            p.add_argument(
                f"--{name.replace('_', '-')}",
                type=int if name == "max_iterations" else float,
                dest=name,
                default=None,
            )
    p.add_argument("--step-iterations", type=int, default=None,
                   help="iterations per step (iterative mode)")
    p.add_argument("--max-total-iterations", type=int, default=None,
                   help="total iteration budget across all steps (iterative mode)")
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

        # Build base params from CLI args
        base_params_dict = {n: getattr(args, n) for n in PARAM_FIELDS if getattr(args, n) is not None}
        base_params = PlacementParams.from_dict(base_params_dict)

        # Determine step iterations and total budget
        step_iters = args.step_iterations
        total_iters = args.max_total_iterations
        iterative = step_iters is not None or total_iters is not None

        if iterative:
            step_iters = step_iters or 50
            total_iters = total_iters or 1000
            if step_iters < 1:
                raise ValueError("--step-iterations must be >= 1")
            if total_iters < 1:
                raise ValueError("--max-total-iterations must be >= 1")

        by_uuid = {fp.uuid: fp for fp in model.footprints if fp.uuid}
        locked = sum(fp.locked for fp in model.footprints)
        print(f"loaded {in_path}: {len(model.footprints)} footprints ({locked} locked)")

        total_iterations_used = 0
        total_moved = 0

        while True:
            # Create params for this step
            if iterative:
                step_params = PlacementParams(
                    repulsion_kr=base_params.repulsion_kr,
                    attraction_ka=base_params.attraction_ka,
                    ideal_length_mm=base_params.ideal_length_mm,
                    max_iterations=step_iters,
                    convergence_eps_mm=base_params.convergence_eps_mm,
                    demo_jitter_mm=base_params.demo_jitter_mm,
                    stub=base_params.stub,
                )
            else:
                step_params = base_params

            proposal = run_placement(model, step_params)
            total_iterations_used += proposal.iterations

            if not proposal.deltas:
                print("placement: no movement (already converged or nothing movable)")
                break

            # Apply deltas to model for next iteration
            model = apply_deltas(model, proposal.deltas)

            # Apply deltas to sexpr tree for output
            for uuid_, delta in proposal.deltas.items():
                dx, dy, da = delta[0], delta[1], delta[2]
                tree = nudge_footprint_by_uuid(tree, uuid_, dx, dy, da)

            total_moved += len(proposal.deltas)
            print(
                f"step: moved={len(proposal.deltas)} iterations={proposal.iterations} "
                f"max_disp={proposal.final_max_disp_mm:.3f}mm elapsed={proposal.elapsed_s:.2f}s"
            )
            for uuid_, (dx, dy, *_) in proposal.deltas.items():
                fp = by_uuid[uuid_]

            # Demo jitter mode (iterations=0) should not iterate further
            if proposal.iterations == 0:
                break

            # Check convergence / budget
            if proposal.final_max_disp_mm < base_params.convergence_eps_mm:
                print("converged (max displacement < convergence_eps_mm)")
                break
            if iterative and total_iterations_used >= total_iters:
                print(f"reached max total iterations ({total_iterations_used}/{total_iters})")
                break
            if not iterative:
                break
                label = fp.ref or uuid_[:8]
                print(
                    f"  {label}: ({fp.x_mm:.2f},{fp.y_mm:.2f}) -> "
                    f"({fp.x_mm + dx:.2f},{fp.y_mm + dy:.2f}) mm"
                )

            # Check convergence / budget
            if proposal.final_max_disp_mm < base_params.convergence_eps_mm:
                print("converged (max displacement < convergence_eps_mm)")
                break
            if iterative and total_iterations_used >= total_iters:
                print(f"reached max total iterations ({total_iterations_used}/{total_iters})")
                break
            if not iterative:
                break

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(to_sexpr(tree) + "\n", encoding="utf-8")

        print(
            f"placement: total_moved={total_moved} total_iterations={total_iterations_used} "
            f"final_max_disp={proposal.final_max_disp_mm:.3f}mm"
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
