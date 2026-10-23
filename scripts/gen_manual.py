#!/usr/bin/env python3
"""Generate the self-contained placement user manual.

The manual is one HTML file with prose, generated before/after board SVGs,
and metrics from three real placement runs. No screenshots and no Quarto.

Usage:
    python scripts/gen_manual.py [--out ui/manual.html] [--check]
    python scripts/gen_manual.py --list
    python scripts/gen_manual.py --only attraction collision
"""

from __future__ import annotations

import argparse
import html
import math
import pathlib
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np

from kicad_autorouter.board_model import (
    BoardModel,
    Footprint,
    Pad,
    Point,
    apply_deltas,
)
from kicad_autorouter.placement import (
    PlacementParams,
    _collect_pad_nodes,
    _footprint_courtyard_polygon,
    run_placement,
)
from kicad_autorouter.svg_render import render_board_svg

DEFAULT_OUT = REPO_ROOT / "ui" / "manual.html"

LEGALIZE = PlacementParams(
    attraction_k_N=0.008,
    wall_k_N=0.08,
    damping_Ns_m=0.12,
    wall_halo_m=0.004,
    dt_s=1e-3,
    wall_exponent=2.0,
    convergence_eps_m=2e-5,
    stall_window=50,
)


def _fp(
    ref: str,
    uuid: str,
    x: float,
    y: float,
    nets: Sequence[str],
    *,
    locked: bool = False,
    hw: float = 3.0,
    hh: float = 2.0,
) -> Footprint:
    """Two-pad 6x4 mm test part with a board-space courtyard."""
    pads = tuple(
        Pad(
            number=str(i + 1),
            net_name=net,
            position=Point(x + (i - (len(nets) - 1) / 2.0) * 2.0, y),
            size_mm=(1.0, 1.0),
            shape="circle",
            angle_deg=0.0,
            pad_type="smd",
            drill_mm=0.0,
            layers=("F.Cu",),
        )
        for i, net in enumerate(nets)
    )
    return Footprint(
        ref=ref,
        footprint_id="test:R",
        layer="F.Cu",
        x_mm=x,
        y_mm=y,
        angle_deg=0.0,
        pads=pads,
        courtyard=(
            (Point(x - hw, y - hh), Point(x + hw, y - hh)),
            (Point(x + hw, y - hh), Point(x + hw, y + hh)),
            (Point(x + hw, y + hh), Point(x - hw, y + hh)),
            (Point(x - hw, y + hh), Point(x - hw, y - hh)),
        ),
        locked=locked,
        uuid=uuid,
        ghost=False,
        ghost_attractions=(),
    )


def _board(footprints: Sequence[Footprint], *, edge: float = 30.0) -> BoardModel:
    return BoardModel(
        footprints=tuple(footprints),
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in footprints},
        edge_cuts=(
            (Point(-edge, -edge), Point(edge, -edge)),
            (Point(edge, -edge), Point(edge, edge)),
            (Point(edge, edge), Point(-edge, edge)),
            (Point(-edge, edge), Point(-edge, -edge)),
        ),
        footprint_region={fp.uuid: 0 for fp in footprints},
    )


def _overlap_area(
    model: BoardModel, deltas: dict, uuids: tuple[str, str]
) -> float:
    moved = apply_deltas(model, deltas)
    pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
    pos = np.array([(pad.position.x_mm, pad.position.y_mm) for pad in pads])
    polys = []
    for uuid in uuids:
        fp = next(f for f in moved.footprints if f.uuid == uuid)
        polys.append(_footprint_courtyard_polygon(
            fp, pad_positions=pos, pad_indices=uuid_to_idx.get(uuid, [])))
    if polys[0].is_empty or polys[1].is_empty:
        return 0.0
    return polys[0].intersection(polys[1]).area


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    note: str
    build: Callable[[], tuple[BoardModel, set[str] | None, PlacementParams]]
    check: Callable[[BoardModel, dict, object], str]


def _attraction() -> tuple[BoardModel, set[str], PlacementParams]:
    a = _fp("A", "uuid-a", -8.0, 0.0, ("SIG", "N-A2"), locked=True)
    b = _fp("B", "uuid-b", 8.0, 0.0, ("SIG", "N-B2"))
    return _board([a, b], edge=24.0), {"uuid-b"}, LEGALIZE


def _check_attraction(model: BoardModel, deltas: dict, prop) -> str:
    dx = deltas["uuid-b"][0]
    overlap = _overlap_area(model, deltas, ("uuid-a", "uuid-b"))
    assert dx < -1.0, f"attraction should pull B left by >1 mm, got {dx:.3f} mm"
    assert overlap < 0.1, f"attraction example must stay legal, overlap {overlap:.3f} mm2"
    return f"B moved {abs(dx):.2f} mm toward A; final courtyard overlap {overlap:.2f} mm²."


def _collision() -> tuple[BoardModel, set[str], PlacementParams]:
    a = _fp("A", "uuid-a", 0.0, 0.0, ("N-A1", "N-A2"))
    b = _fp("B", "uuid-b", 1.0, 0.0, ("N-B1", "N-B2"))
    return _board([a, b], edge=24.0), {"uuid-a", "uuid-b"}, LEGALIZE


def _check_collision(model: BoardModel, deltas: dict, prop) -> str:
    before = _overlap_area(model, {}, ("uuid-a", "uuid-b"))
    after = _overlap_area(model, deltas, ("uuid-a", "uuid-b"))
    assert before > 10.0, f"collision example should start deeply overlapped, got {before:.2f}"
    assert after < 0.1, f"collision example should clear, overlap {after:.3f} mm2"
    return f"Overlap fell from {before:.1f} mm² to {after:.2f} mm²; the courtyard wall found the shortest escape."


def _cage() -> tuple[BoardModel, set[str], PlacementParams]:
    footprints = [
        _fp("L", "uuid-l", -7.0, 0.0, ("N-L1", "N-L2"), locked=True),
        _fp("R", "uuid-r", 7.0, 0.0, ("N-R1", "N-R2"), locked=True),
        _fp("T", "uuid-t", 0.0, 6.0, ("N-T1", "N-T2"), locked=True),
        _fp("B", "uuid-b", 0.0, -6.0, ("N-B1", "N-B2"), locked=True),
        _fp("M", "uuid-m", 0.0, 0.0, ("N-M1", "N-M2")),
    ]
    return _board(footprints, edge=20.0), {"uuid-m"}, LEGALIZE


def _check_cage(model: BoardModel, deltas: dict, prop) -> str:
    dx, dy, da = deltas["uuid-m"]
    move = math.hypot(dx, dy)
    assert move < 1.0, f"caged part should barely move, got {move:.2f} mm"
    assert abs(da) < 10.0, f"caged part should barely rotate, got {da:.1f} deg"
    assert not prop.overlapping_pairs, f"cage produced overlaps: {prop.overlapping_pairs}"
    return (
        f"The caged part moved {move:.2f} mm and rotated {da:.1f}°; "
        "the locked walls held it in place."
    )


SCENARIOS = (
    Scenario(
        "attraction",
        "Attraction: a net pulls two parts together",
        "Two parts share one signal net. The right part is free; the left part is locked.",
        "Attraction is constant-magnitude. The spring keeps pulling after contact, so the courtyard wall is what finally stops the part; the run ends at the iteration budget with the part held at the wall.",
        _attraction,
        _check_attraction,
    ),
    Scenario(
        "collision",
        "Collision: overlapping courtyards push apart",
        "Two 6×4 mm parts start 1 mm apart, a 5 mm-deep courtyard overlap.",
        "Every contact reports a signed separation. The deepest sub-contact wins, so stacked pad/pin paths cannot multiply the force.",
        _collision,
        _check_collision,
    ),
    Scenario(
        "cage",
        "Legalize: a part held by locked neighbors",
        "A movable part sits in a four-wall cage of locked parts.",
        "Locked parts are anchors: they feel and apply forces but never move. A legalized part settles with only a small nudge.",
        _cage,
        _check_cage,
    ),
)


@dataclass(frozen=True)
class Result:
    scenario: Scenario
    before_svg: str
    after_svg: str
    stop_reason: str
    iterations: int
    max_move_mm: float
    residual_force_n: float
    legal: bool
    overlaps: tuple
    note: str


def _run(scenario: Scenario) -> Result:
    model, movable, params = scenario.build()
    prop = run_placement(model, params, movable_uuids=movable)
    deltas = prop.deltas
    max_move = max(
        (math.hypot(dx, dy) for dx, dy, _ in deltas.values()), default=0.0
    )
    note = scenario.check(model, deltas, prop)
    return Result(
        scenario=scenario,
        before_svg=render_board_svg(model, title=f"{scenario.id} — before"),
        after_svg=render_board_svg(model, proposal=prop, title=f"{scenario.id} — after"),
        stop_reason=prop.stop_reason,
        iterations=prop.iterations,
        max_move_mm=max_move,
        residual_force_n=prop.residual_max_force_N,
        legal=prop.legality_is_legal,
        overlaps=tuple(prop.overlapping_pairs),
        note=note,
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KiCad AutoRouter — Placement Manual</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0b1117; color: #d7e1ea;
         font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 980px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
  h1, h2, h3 { color: #f3f7fa; line-height: 1.2; }
  h1 { font-size: 2rem; margin: 0 0 .4rem; }
  h2 { margin-top: 2.6rem; border-top: 1px solid #24313c; padding-top: 1.4rem; }
  h3 { margin-top: 1.7rem; }
  p, li { max-width: 78ch; }
  a { color: #7ee0a0; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  code { background: #141d25; border: 1px solid #24313c; border-radius: 4px;
         padding: .08em .35em; color: #d7e1ea; }
  pre { background: #141d25; border: 1px solid #24313c; border-radius: 7px;
        padding: .85rem 1rem; overflow: auto; }
  .lede { color: #9fb0bf; font-size: 1.02rem; }
  .example { margin-top: 2.2rem; }
  .boards { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin: 1rem 0; }
  .board { height: 260px; background: #0f171e; border: 1px solid #24313c;
           border-radius: 7px; overflow: hidden; }
  .board svg { display: block; width: 100%; height: 100%; }
  .board-label { color: #8ea0af; font-size: .72rem; letter-spacing: .08em;
                 text-transform: uppercase; margin: .35rem 0 .2rem; }
  table { border-collapse: collapse; width: 100%; margin: .8rem 0 1rem; }
  th, td { border: 1px solid #24313c; padding: .35rem .55rem; text-align: left; }
  th { background: #141d25; color: #b8c7d3; font-weight: 600; }
  .note { border-left: 3px solid #7ee0a0; background: #101920;
          padding: .65rem .9rem; margin: .8rem 0 0; color: #c8d5df; }
  .muted { color: #8ea0af; }
  @media (max-width: 720px) { .boards { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<main>
<h1>Placement Manual</h1>
<p class="lede">A short, code-generated guide to the force-spring placement workflow.
Every board below is rendered by the current engine; there are no screenshots.</p>

<h2>Start here</h2>
<pre><code>./start_server.sh
# open http://127.0.0.1:8000</code></pre>
<p>Load a <code>.kicad_pcb</code> board, open the Placement panel, and choose a
preset. <strong>Step</strong> advances the solver and previews the result;
<strong>Accept</strong> writes the previewed pose into the working board.
<strong>Run</strong> repeats the step/accept loop until it settles or hits a limit.</p>

<h2>Reading a placement run</h2>
<table>
<tr><th>Signal</th><th>Meaning</th></tr>
<tr><td><code>converged</code></td><td>Motion is below the settle threshold and net force is quiet. Safe to accept.</td></tr>
<tr><td><code>stalled</code></td><td>No net progress for the stall window, but forces remain. A part is wedged or chattering; move a neighbor or add room.</td></tr>
<tr><td><code>budget</code></td><td>The iteration cap was reached while work remained. Treat it like stalled, not settled.</td></tr>
<tr><td><code>empty</code></td><td>Nothing movable or nothing to integrate.</td></tr>
<tr><td>legal / overlap</td><td>Post-hoc courtyard and board-boundary check on the final merged pose.</td></tr>
</table>
<p>Pinned parts and locked footprints are anchors: they apply forces but never
move. Ghosts are non-copper anchors — walls that push parts out of keepouts,
or attractors that pull a named target.</p>

<h2>Generated examples</h2>
<p class="muted">All three use the Legalize preset: <code>attraction_k_N=0.008</code>,
<code>wall_k_N=0.08</code>, <code>damping_Ns_m=0.12</code>,
<code>wall_halo_m=0.004</code>, <code>wall_exponent=2</code>, 1 ms steps.
Regenerate with <code>.venv/bin/python scripts/gen_manual.py</code>.
Each example asserts its own invariant; generation fails if the engine drifts.</p>
{examples}

<h2>Go deeper</h2>
<ul>
<li><a href="/docs.html?doc=docs/si_force_laws.md">SI force laws</a> — normative formulas and defaults.</li>
<li><a href="/docs.html?doc=docs/physics_primer.qmd">Physics primer</a> — force-law intuition and parameter tradeoffs.</li>
<li><a href="/docs.html?doc=docs/autorouting_glossary.md">Autorouting glossary</a> — pipeline stages and terminology.</li>
</ul>
</main>
</body>
</html>
"""


def _example_html(result: Result, index: int) -> str:
    s = result.scenario
    overlaps = (
        ", ".join(f"{a}/{b}: {area:.2f} mm²" for a, b, area in result.overlaps)
        or "none"
    )
    legal = "legal" if result.legal else "needs attention"
    return f"""
<section class="example">
<h3>{index}. {html.escape(s.title)}</h3>
<p>{html.escape(s.description)}</p>
<div class="boards">
  <div><div class="board-label">before</div><div class="board">{result.before_svg}</div></div>
  <div><div class="board-label">after</div><div class="board">{result.after_svg}</div></div>
</div>
<table>
<tr><th>Stop reason</th><th>Iterations</th><th>Max move</th><th>Residual force</th><th>Legality</th></tr>
<tr><td>{html.escape(result.stop_reason)}</td><td>{result.iterations}</td>
<td>{result.max_move_mm:.2f} mm</td><td>{result.residual_force_n:.2e} N</td>
<td>{legal}</td></tr>
<tr><th>Overlaps</th><td colspan="4">{html.escape(overlaps)}</td></tr>
</table>
<p class="note">{html.escape(result.note)}</p>
<p class="muted">{html.escape(s.note)}</p>
</section>
"""


def render(results: Sequence[Result]) -> str:
    examples = "".join(
        _example_html(result, i + 1) for i, result in enumerate(results)
    )
    return PAGE.replace("{examples}", examples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="HTML output path (default: ui/manual.html)")
    parser.add_argument("--only", nargs="*", choices=[s.id for s in SCENARIOS],
                        help="run only these examples")
    parser.add_argument("--check", action="store_true",
                        help="run and assert examples without writing HTML")
    parser.add_argument("--list", action="store_true", help="list example ids")
    args = parser.parse_args(argv)

    if args.list:
        for scenario in SCENARIOS:
            print(scenario.id)
        return 0

    selected = [
        scenario for scenario in SCENARIOS
        if not args.only or scenario.id in args.only
    ]
    if not selected:
        print("nothing selected", file=sys.stderr)
        return 1

    results = []
    for scenario in selected:
        result = _run(scenario)
        print(f"{scenario.id}: {result.stop_reason}, {result.iterations} iters, "
              f"{result.max_move_mm:.2f} mm")
        results.append(result)

    if args.check:
        return 0

    out = pathlib.Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(results), encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
