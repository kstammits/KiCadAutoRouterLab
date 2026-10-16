# AGENTS.md — KiCad AutoRouter Lab

Magnetic-force placement + grid routing for KiCad `.kicad_pcb` boards, with a
browser workflow UI. Works on real boards, DRC-validated.

## Layout

- `src/kicad_autorouter/` — engine: `placement.py` (force-spring sim),
  `board_model.py` (parse model, power/ground helpers), `routing/` (grid,
  obstacles, A* router, pipeline, pour/stitch/power), `io.py`, `sexpr.py`,
  `drc.py`, `validate.py`, `ghost.py`, `svg_render.py`.
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

## Context sources (not vendored)

`docs/kicad/` + `docs/wikipedia/` were removed to keep the repo small.
Restore locally via the recipes in `docs/README.md`, or read upstream:
KiCad 10.0 manuals at `https://docs.kicad.org/10.0/en/`
(`kicad/`, `pcbnew/`, `eeschema/`, `cli/`);
Wikipedia: `Autorouter`, `Maze_routing`, `Lee_algorithm`,
`Force-directed_graph_drawing`, `Barnes%E2%80%93Hut_simulation`,
`A*_search_algorithm`. The digested guidance is `docs/autorouting_glossary.md`.
