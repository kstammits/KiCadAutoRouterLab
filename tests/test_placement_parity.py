"""Parity: courtyard shape-cache + broadphase must not change physics.

SCOPE — READ BEFORE EXTENDING: this is a refactor tripwire, NOT a physics
correctness bar. It pins the implementation against its own past outputs;
the pre-optimization physics was never independently validated ("golden"
means historical, not correct). Behavioral correctness lives in
tests/test_magnet.py::TestMagnetHeuristics (clustering across gains and
layouts, separation, determinism). If a deliberate physics change moves
trajectories, update the golden file AND justify the change against the
magnet behaviors — never regenerate golden from the new code alone to make
red go green.

Because contact forces are stiff (1/d singularities), even 1-ulp input
differences compound over dozens of iterations — verified during
development: an early version with a too-tight cull bound diverged by
millimeters. So this test pins trajectories against golden values captured
from the pre-optimization code (30 fixed-budget iterations, tight eps so
the full budget runs).

Golden file: tests/fixtures/placement_parity_golden.json
NOTE: golden regenerated 2026-10-09 after layer-aware courtyard collision
landed (same-layer + THT pairs collide; cross-layer SMD-SMD exempt).
tube111 (~70% backside) trajectories changed as expected; magnet suite
(all-F.Cu) is unchanged.
(tube111: multi-region; ghost: ghost branch; dccf: large board).
Regenerate ONLY by running the capture snippet against code whose physics
is independently trusted — never from the optimized code itself:

    .venv/bin/python -c "
    import json, sys; sys.path.insert(0, 'src')
    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.sexpr import parse_file
    from kicad_autorouter.placement import PlacementParams, run_placement
    out = {}
    for name, fx in [('tube111', 'tests/fixtures/tube111.kicad_pcb'),
                     ('ghost', 'tests/fixtures/ghost_test.kicad_pcb'),
                     ('dccf', 'tests/fixtures/DCCF.sved.kicad_pcb')]:
        m = board_model(parse_file(fx))
        r = run_placement(m, PlacementParams(max_iterations=30, convergence_eps_mm=1e-12))
        out[name] = {'iterations': r.iterations,
                     'deltas': {u: list(d) for u, d in sorted(r.deltas.items())},
                     'forces': {u: list(f) for u, f in sorted(r.forces.items())}}
        print(name, r.iterations, len(r.deltas))
    json.dump(out, open('tests/fixtures/placement_parity_golden.json', 'w'), indent=1)
    "
"""

import json
from pathlib import Path

from kicad_autorouter.board_model import board_model
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.sexpr import parse_file

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "placement_parity_golden.json"

CASES = {
    "tube111": FIXTURES / "tube111.kicad_pcb",
    "ghost": FIXTURES / "ghost_test.kicad_pcb",
    "dccf": FIXTURES / "DCCF.sved.kicad_pcb",
}


def _load_golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def test_courtyard_opt_parity_trajectories():
    """Deltas + forces match golden to 1e-9 (bit-identical in practice)."""
    gold = _load_golden()
    params = PlacementParams(max_iterations=30, convergence_eps_mm=1e-12)
    for name, path in CASES.items():
        model = board_model(parse_file(path))
        result = run_placement(model, params)
        g = gold[name]
        assert result.iterations == g["iterations"], name
        assert set(result.deltas) == set(g["deltas"]), name
        for uuid, delta in result.deltas.items():
            for a, b in zip(delta, g["deltas"][uuid]):
                assert abs(a - b) <= 1e-9, (name, uuid, delta, g["deltas"][uuid])
        for uuid, force in result.forces.items():
            for a, b in zip(force, g["forces"][uuid]):
                assert abs(a - b) <= 1e-9, (name, uuid)
