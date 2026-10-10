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
NOTE: golden regenerated 2026-10-10 after deliberate physics fixes —
pair-loop ordering (movable-vs-earlier-immobile), bounded boundary push,
and courtyard-outline via polygonize/unary_union (DCCF 87 closed/0 fallback).
Justification: tests/test_magnet.py (12 passed) + test_placement_pairs.py
+ test_placement_layers.py (9 passed) all green on the new physics.
NOTE: forces entries regenerated 2026-10-10 after deliberate REPORTING
change (no physics change): proposals now report initial (next-step) forces
F0 evaluated at the committed board instead of the residual F1 at the
preview end, and the SVG overlay anchors them on the committed positions.
Deltas in this file are byte-identical before/after (the trajectories loop
above passed unmodified); only "forces" values changed. Justification:
test_forces_predict_next_step_not_residual +
test_single_step_delta_follows_reported_force in
tests/test_placement.py::TestForceComponents, magnet/layers/pairs suites
green.
NOTE: full golden regenerated 2026-10-10 after deliberate dynamics
retune (placement physics, all justified against the behavioral bar):
lowered courtyard/boundary defaults (kc 10000->2000, kb 500000->20000;
magnitudes were discarded by step caps, only overshoot remains),
max_step 2.0->1.0mm (no more vaulting across the 3mm halo in one step),
per-iteration rotation cap max_dangle_deg=15 (closed-form Kabsch fit,
rotate-back-to-cap about the centroid + recenter, so translation is
bit-identical with the cap on or off; 15deg allows full THT
reorientation over a run, unlike 3deg which starved magnet clustering),
and contact-weighted overlap pushes (pads on the contact side take
more; weights average exactly 1 so totals are conserved; halo nudges
stay uniform broadcasts). Justification: tests/test_magnet.py (all
heuristics incl. clustering ratios), tests/test_placement_layers.py,
tests/test_placement_pairs.py (incl. rotation-cap, weighting-conservation
and separation pins) all green; cross-scale spot checks (lone-pair
attraction, DCCF U2 single-step force/delta agreement) sane.
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
