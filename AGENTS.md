# AGENTS.md — KiCad AutoRouter Lab

Magnetic-force placement + grid routing for KiCad `.kicad_pcb` boards, with a
browser workflow UI. Works on real boards, DRC-validated.

## Layout

- `src/kicad_autorouter/` — engine: `placement/` package (force-spring
  sim: `force.py`, `runner.py`, `contact.py`, `rigid.py`, `params.py`,
  `model.py`, `integrate.py`), `board_model.py` (parse model, power/ground
  helpers), `routing/` (grid, obstacles, A* router, pipeline,
  pour/stitch/power), `io.py`, `sexpr.py`, `drc.py`, `validate.py`,
  `ghost.py`, `svg_render.py`.
- `ui/server.py` — stdlib-HTTP API + state (no framework); `ui/index.html` —
  the whole frontend (single file, inline CSS/JS).
- `tests/fixtures/` — `tube111.*` (105 parts, 73 nets, reference board),
  `DCCF.sved.*` (audio board, split rails), `minimal.*` (4 parts, no nets).
  Don't add fixtures without a test that needs them.
- `scripts/` — `benchmark_*.py` (perf), `run_autoroute.py` (headless),
  `regen_*.sh` (quarto/pytest reports), `clean.sh` (scratch cleanup).
- `docs/` — `autorouting_glossary.md` (domain guidance),
  `si_force_laws.md` + `physics_primer.qmd` (placement physics),
  `benchmarks/*.json` (latest per family only), `figures/` (generated PNGs).

## Commands

- Server: `./start_server.sh [port]` (default 8000) or `.venv/bin/python ui/server.py`.
- Tests: `.venv/bin/python -m pytest tests/ -q` (pyproject sets
  `testpaths` + `pythonpath=["src"]`, so plain `pytest` works inside the venv).
- Markers: `unit params physics cli server regression minimal dccf`
  (see `[tool.pytest.ini_options]`). Always use `.venv/bin/python` —
  system `python` lacks project deps (`scipy`, `shapely`, …) and fails
  at collection with `ModuleNotFoundError`.
- Cleanup: `scripts/clean.sh [--all]` (gitignored scratch only).

## Conventions

- **SI units in the engine** (N, m, s); the UI converts at the boundary
  (`toDisplay`/`fromDisplay` in `index.html`, `display: {scale}` in param defs).
- **No `print` telemetry in library code**, no function-attribute state
  (a `_shape_counts` counter once accumulated across runs — removed).
  Debug output goes through return values or `logging`, never stdout.
- `PlacementParams` / `RoutingParams` round-trip via `to_dict`/`from_dict`;
  `from_dict` drops unknown keys, so the UI can POST partial params.
- Placement is region-based (`_run_region_simulation` per board region);
  `run_placement` merges. Ghost footprints are fixed anchors, never movable.
- Power membership = keyword match OR user override set (`extra_power`,
  toggled in the nets table via `/api/nets/power`); ground stays
  keyword-only. Power wins on overlap (e.g. `GND_PWR`).
- KiCad file formats are S-expressions, not JSON (`sexpr.py`).
  `kicad-cli` lives at
  `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` (not on PATH).

## Placement dynamics

- One wall law (`_wall_mag` in `contact.py`): part courtyards push apart;
  the board region polygon is an inverted courtyard pushing inside with
  the same `wall_k_N` / `wall_halo_m`. 
- Region-based placement; ghost footprints are fixed anchors, never movable.
- Stop reasons: `converged` (quiet + settled forces), `stalled` (no window
  progress, forces remain), `budget` (max iters reached), `empty`.
- Force causes: repulsion / attraction / courtyard (includes board wall) / ghost.
- `tests/fixtures/2B.kicad_pcb` is UNTRACKED — do NOT `git add`/`git commit`;
  write synthetic regression tests instead (see `test_straddling_ghost_wall_...`,
  `test_loaded_contact_settles...`, `test_quiet_without_progress...`).
- DCCF overlap allowlist (`KNOWN_DCCF_OVERLAPS_200`) re-characterized
  2026-10-20 (19 pairs). Magnet boards scatter INSIDE the outline; wall
  test asserts courtyards fully inside + 0.5mm under 25x gain.

Detailed physics constants (blend ratios, cache thresholds, Kabsch caps,
stall multiplier) live in `si_force_laws.md` / code comments.

## Low-powered / fast placement mode

For quick iterations (not production quality), tune:

| Param | Default | Fast-mode | Effect |
|-------|---------|-----------|--------|
| `max_iterations` | 1000 | 50–200 | caps runtime |
| `dt_s` | 1 ms | 2 ms | larger steps |
| `convergence_eps_m` | 1 µm | 10–50 µm | looser settle |
| `stall_window` | 100 | 20–50 | stops sooner |
| `wall_k_N` | 0.09 | 0.02–0.05 | weaker pushes |

Low-power runs report `stalled`/`budget` more often — expected, not a bug.
Treat as "needs attention," not settled.

## Fixtures for fast testing

- `minimal.*` (4 parts, no nets) — fastest, no `kicad-cli` needed.
- `tube111.*` / `DCCF.sved.*` — heavy (105/97 parts); avoid for quick loops.
- Never use untracked `2B.kicad_pcb`.

## Context sources (not vendored)

`docs/kicad/` + `docs/wikipedia/` removed. Restore via `docs/README.md`
recipes, or read upstream: KiCad 10.0 manuals at
`https://docs.kicad.org/10.0/en/` (`kicad/`, `pcbnew/`, `eeschema/`, `cli/`);
Wikipedia: `Autorouter`, `Maze_routing`, `Lee_algorithm`,
`Force-directed_graph_drawing`, `Barnes%E2%80%93Hut_simulation`,
`A*_search_algorithm`. Digested guidance in `docs/autorouting_glossary.md`.