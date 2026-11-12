# AGENTS.md — KiCad AutoRouter Lab

Magnetic-force placement + grid routing for KiCad `.kicad_pcb` boards, with a
browser workflow UI. Works on real boards, DRC-validated.

## Layout

- `src/kicad_autorouter/` — engine: `placement/` (force-spring sim +
  align/depenetrate/legality), `routing/` (grid, A* router, pipeline,
  pour/stitch/shove, smooth/polish, corridor), `board_model/` (model,
  parse, query, labels), plus `io.py`, `sexpr.py`, `drc.py`, `strategy.py`,
  `ghost.py`, `mass.py`, `svg_render.py`.
- `ui/` — `server.py` (stdlib-HTTP dispatch over `server_state` /
  `server_jobs` + `handlers/`), `index.html` + `ui/app-*.js` (shared
  globals, no modules; load order in `index.html`) + `style.css`;
  `docs.html` — docs viewer.
- `tests/fixtures/` — `tube111.*` (105 parts, 73 nets), `DCCF.sved.*`
  (audio, split rails), `minimal.*` (4 parts, no nets), `ghost_test.*`,
  `minimal_placed.*`. Don't add fixtures without a test that needs them.
- `scripts/` — `benchmark_*.py` (results land untracked in
  `docs/benchmarks/` — see its README), `run_autoroute.py` (headless),
  `calibrate_si.py`, `sweep_*.py`, `plot_force_laws.py`,
  `gen_manual.py` (`ui/manual.html`), `regen_docs.sh`, `clean.sh`.
- `docs/` — glossary, `si_force_laws.md` + `physics_primer.qmd`, `roadmap.md`,
  `figures/` (generated). Untracked locally: `benchmarks/*.json`
  (regenerate via `scripts/benchmark_*.py`), `tests.md`,
  `emit_gate_smoother_proposal.md`.

## Commands

- Server: `./start_server.sh [port]` (default 8000).
- Tests: `.venv/bin/python -m pytest tests/ -q` (always `.venv` — system
  `python` lacks deps and fails at collection). Fast loop:
  `-m "minimal or unit or (dccf and not slow)"` (`slow` = >3s).
  Deps are pinned in `requirements-lock.txt` (`pip install -e ".[dev]"
  --constraint requirements-lock.txt`); re-freeze from a clean green venv
  when bumping.
- Docs: `./scripts/regen_docs.sh` (figures + manual + primer);
  `gen_manual.py` alone rebuilds just the manual.
- JS syntax: `./scripts/check_js.sh` (`node --check` all `ui/app-*.js`).
- Markers: `unit params physics cli server regression minimal dccf slow`.
- Cleanup: `scripts/clean.sh [--all]` (gitignored scratch only).

## Conventions

- **SI units in the engine** (N, m, s); the UI converts at the boundary
  (`toDisplay`/`fromDisplay` in `app-params.js`).
- **No `print` telemetry in library code**, no function-attribute state.
  Debug output via return values or `logging`, never stdout.
- **Comments describe current behavior and rationale**, not history —
  that belongs in git log or `docs/roadmap.md`.
- `PlacementParams` / `RoutingParams` round-trip via `to_dict`/`from_dict`;
  `from_dict` drops unknown keys, so the UI can POST partial params.
- Placement is region-based and merged by `run_placement`. Ghosts are fixed
  anchors, never movable.
- Power = keyword match OR `extra_power` override (`/api/nets/power`);
  ground stays keyword-only. Power wins on overlap.
- KiCad files are S-expressions, not JSON (`sexpr.py`). `kicad-cli` is at
  `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` (not on PATH).

## Placement dynamics

- One wall law (`_wall_mag` in `contact.py`): courtyards push apart; the
  board region polygon is an inverted courtyard with the same
  `wall_k_N` / `wall_halo_m`.
- Stop reasons: `converged` (quiet + settled), `stalled` (no progress,
  forces remain), `budget` (max iters), `empty`. Illegal `converged` runs
  report as `stalled`. Cross-layer pairs use pin keep-out, not courtyard
  overlap — opposite-side SMD may stack.
- Force causes: repulsion / attraction / courtyard (incl. board wall) /
  ghost / tangential.
- DCCF overlap audit baselines pre-existing squeezes; new or materially
  worsened >1 mm² overlaps fail. Magnet boards scatter inside the outline.
  Physics constants live in `si_force_laws.md` / code comments.

## Low-powered / fast placement mode

| Param | Default | Fast-mode | Effect |
|-------|---------|-----------|--------|
| `max_iterations` | 1000 | 50–200 | caps runtime |
| `dt_s` | 1 ms | 2 ms | larger steps |
| `convergence_eps_m` | 2 µm | 10–50 µm | looser settle |
| `stall_window` | 100 | 20–50 | stops sooner |
| `wall_k_N` | 0.1 | 0.02–0.05 | weaker pushes |

Fast runs report `stalled`/`budget` more often — expected ("needs
attention," not settled).

## Fixtures for fast testing

- `minimal.*` (4 parts, no nets) — fastest, no `kicad-cli` needed.
- `tube111.*` / `DCCF.sved.*` — heavy (~100 parts); avoid for quick loops.

## Context sources (not vendored)

`docs/kicad/` + `docs/wikipedia/` restore via `docs/README.md` recipes, or
read upstream: KiCad 10.0 manuals at `https://docs.kicad.org/10.0/en/`;
Wikipedia: `Autorouter`, `Maze_routing`, `Lee_algorithm`,
`Force-directed_graph_drawing`, `Barnes%E2%80%93Hut_simulation`,
`A*_search_algorithm`. Digested guidance: `docs/autorouting_glossary.md`.
