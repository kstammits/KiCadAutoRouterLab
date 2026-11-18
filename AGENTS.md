# AGENTS.md — Karltron AutoRouter

Magnetic-force placement + grid routing for KiCad `.kicad_pcb` boards, with a
browser workflow UI.

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
  (audio, split rails), `minimal.*` (4 parts, no nets; needs no
  `kicad-cli`), `ghost_test.*`, `minimal_placed.*`. Don't add fixtures
  without a test that needs them.
- `scripts/` — `gen_manual.py` (`ui/manual.html`), `regen_docs.sh`
  (manual rebuild wrapper), `check_js.sh`, `clean.sh`.
- `docs/` — glossary, `si_force_laws.md`.

## Commands

- Server: `./start_server.sh [port]` (default 8000).
- Tests: `.venv/bin/python -m pytest tests/ -q` (always `.venv` — system
  `python` lacks deps and fails at collection). Fast loop:
  `-m "minimal or unit or (dccf and not slow)"`.
  Deps are pinned in `requirements-lock.txt`; re-freeze from a clean
  green venv when bumping.
- Docs: `./scripts/regen_docs.sh [gen_manual args]` rebuilds the manual
  (`--check` asserts examples without writing).
- JS syntax: `./scripts/check_js.sh` (`node --check` all `ui/app-*.js`).
- Markers: `unit params physics cli server regression minimal dccf slow`.
- Cleanup: `scripts/clean.sh [--all]` (gitignored scratch only).

## Conventions

- **SI units in the engine** (N, m, s); the UI converts at the boundary
  (`toDisplay`/`fromDisplay` in `app-params.js`).
- **Comments describe current behavior and rationale**, not history —
  that belongs in git log.
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
