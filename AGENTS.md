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

## Placement dynamics (state as of 2026-10-20)

- One wall law (`_wall_mag` in `contact.py`): part courtyards push apart;
  the board region polygon is an inverted courtyard pushing inside with
  the same `wall_k_N` / `wall_halo_m`. There is no `boundary_k_N`
  anywhere (removed from params, UI, scripts, docs, figures);
  `from_dict` drops it from old files.
- Ghost walls join a region by courtyard overlap, not centroid
  (`_assign_board_regions` fallback in `board_model.py`): a wall
  straddling the board edge has its centroid outside and would otherwise
  score region -1 and exert nothing.
- Per-pair contact: deepest penetration wins, then its normal blends with
  the prior step (50/50 when aligned, 25/75 prior-dominant when opposing
  — a 180° flip slews instead of snapping). State lives in
  `ForceContext.last_contact` and is pruned when pairs separate. Never
  reintroduce beat-by-margin winner switching; blending is the mechanism.
- Poly-cache rebuild thresholds are tight (20µm / 0.02°) so refresh
  jumps stay small enough to blend out (stale polygons were a flip-flop
  source at 0.1mm).
- Rigid: Kabsch snap per step, rotation halved, 10° cap, 0.1° deadband
  (`MIN_ROT_STEP_RAD`) applied after halving. Translation untouched,
  rotation stays free.
- Convergence: `residual_max_force_N` = max over movable footprints of
  NET force (rigid-aware — per-pad loads cancel across a body in
  contact, so per-pad max could never settle). Stop reasons: `converged`
  (quiet step + settled nets), `stalled` (window-averaged pose shows no
  net progress over a full `stall_window`, default 100, with forces
  remaining: wedged or chattering in place — per-step quiet never arrives
  under contact chatter, so the rule compares window means with tolerance
  `STALL_MEAN_EPS_MULT * convergence_eps_m`, never rest), `budget` (still
  moving), `empty`. A wedged single-part run converging inside overlap is
  honest; all-movable runs are what resolve piles.
- Force causes are four: repulsion / attraction / courtyard (includes the
  board wall) / ghost. SVG overlay + UI legend match; server
  `/api/placement/forces` asserts the same set.
- `tests/fixtures/2B.kicad_pcb` is UNTRACKED by owner request — do NOT
  `git add`/`git commit` it; write synthetic regression tests instead
  (see `test_straddling_ghost_wall_joins_region_and_pushes`,
  `test_loaded_contact_settles_despite_per_pad_loads`,
  `test_quiet_without_progress_stops_stalled`).
- DCCF overlap allowlist (`KNOWN_DCCF_OVERLAPS_200`) re-characterized
  2026-10-20 (19 pairs — reshuffled within the same shared-net wedge
  family by the damped-snap/deadband/blending retime; see its comment).
- Magnet boards scatter INSIDE the outline (like production); the old
  [5,95]² scatter predated the minimal-board regions and piled parts at
  the SW door behind locked MH1 — contain locally, don't widen bounds.
  Wall test asserts courtyards fully inside + 0.5mm under 25x gain.

## Context sources (not vendored)

`docs/kicad/` + `docs/wikipedia/` were removed to keep the repo small.
Restore locally via the recipes in `docs/README.md`, or read upstream:
KiCad 10.0 manuals at `https://docs.kicad.org/10.0/en/`
(`kicad/`, `pcbnew/`, `eeschema/`, `cli/`);
Wikipedia: `Autorouter`, `Maze_routing`, `Lee_algorithm`,
`Force-directed_graph_drawing`, `Barnes%E2%80%93Hut_simulation`,
`A*_search_algorithm`. The digested guidance is `docs/autorouting_glossary.md`.
