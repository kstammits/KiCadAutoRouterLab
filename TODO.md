# KiCad AutoRouter Lab — TODO / Session State

Goal: utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace placement.
Phase: **minimal read-modify-write pass done** (load board → nudge unlocked parts → add demo track → save) + **pcb+sch no-op round trip done** (pure Python: load pair → identity edit → new files → checks). Guidance/glossary doc drafted (`docs/autorouting_glossary.md`); full autorouter logic still pending. System on **KiCad 10** (`kicad-cli` 10.0.7); local docs are KiCad 10. Board-viewer plan for `ui/` agreed (see Active).

## Active / Next Moves (in order)
- [ ] **Extend the autorouter beyond the baby step.** Both interfaces, shared core; SWIG `pcbnew` runtime; PCM packaging = build target only (nothing to package yet).
   - Core stays pure Python (no `pcbnew` import) → testable in `.venv`. **Decision 2026-09-29:** parse-stage *semantics* — absolute/rotation-aware pad positions, nets, zones — are sourced from `pcbnew` via the adapter (`LoadBoard()` typed objects), NOT hand-rolled on top of `sexpr.py`; `sexpr.py` stays a portable structural reader / test double. New: `model.py` (abstract board fed by the adapter), `placement.py` (force-directed nudge, locked=fixed), `routing.py` (maze/Lee, later); `pcbnew_adapter.py` already exists (only place importing `pcbnew`).
  - Entry points: `plugin/` action plugin (`class Nudge(pcbnew.ActionPlugin)`, `.register()`) + `scripts/run_autoroute.py` headless CLI (first pass done).
  - Locked components = native footprint lock `fp.IsLocked()` / `fp.SetLocked(True)`; only unlocked parts move.
  - Runs under bundled Python 3.9: `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3` (verify numpy/networkx availability there).
  - Install dir: `~/Documents/KiCad/10.0/scripting/plugins/<name>/`. PCM scaffold later in `packaging/` (`metadata.json.template` v2 + `build_package.py` stub).
- [ ] **Web UI board viewer** (`ui/`) — render `.kicad_pcb` in-browser to review routing progress without opening KiCad. Decision: in-browser SVG viewer is the review surface; interactive editing (locking parts, params) stays in KiCad / CLI flags. Self-contained — can be built before or alongside routing work. Plan agreed 2026-09-29:
  - Data path: add `export_board_json(board)` to `src/kicad_autorouter/pcbnew_adapter.py` (only module importing `pcbnew`) → compact JSON in mm: bounds, layer list, footprints (ref, x/y, rotation, courtyard, pads via `GetAbsolutePosition()`), tracks (endpoints, width, layer), zone outlines. The adapter's pcbnew objects are the accurate semantic source (no hand-rolled geometry). The pure-Python `sexpr.py` reader is a separate portable structural / test-double layer — not the routing-accurate geometry source.
  - Server runtime: run `ui/server.py` under KiCad bundled Python 3.9 (`/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3`) so `/api/board` calls the adapter in-process (headless `pcbnew` import already proven by baby-step script). `.venv` fallback: viewer endpoint returns 503 "run server with bundled Python"; stage cards still work.
  - Rendering: JSON → client-side SVG, no new deps; pan/zoom via `viewBox`; layer visibility checkboxes. v1 draws board outline + F.Cu/B.Cu tracks + footprint courtyards/ref text/pads (circle/rect by shape) + zone outlines at low opacity. Skip silkscreen/dimensions/drill marks in v1.
  - API: `GET /api/board?path=<rel>` → `{units:"mm", bounds, layers:[{id,name}], footprints:[...], tracks:[...], zones:[...]}`; reuse existing path-traversal guard (resolve vs repo root, reject escapes).
  - v2 (the actual progress-review feature): side-by-side before/after panes — highlight moved footprints + new tracks; optional DRC marker overlay from `validate.py` JSON.
  - Files to touch: `src/kicad_autorouter/pcbnew_adapter.py`, `ui/server.py`, `ui/index.html` (+ small `ui/viewer.js`), `README.md` (run instructions for bundled-Python server), this file.
  - Verify: pad positions must be absolute/rotation-aware — check one known footprint against KiCad; compare rendered track counts to `scripts/run_autoroute.py` console output; test boards = `tests/fixtures/minimal.kicad_pcb`, EuroCard fixture, baby-step output in `experiments/out/`.

## Completed
- [x] **S-expression grammar reference identified** (via GitHub mirror `KiCad/kicad-source-mirror`, branch `master`; GitLab direct access is 403). Raw URL pattern: `https://raw.githubusercontent.com/KiCad/kicad-source-mirror/master/<path>`.
  - `.kicad_pcb` → `pcbnew/pcb_io/kicad_sexpr/`: `pcb_io_kicad_sexpr.{h,cpp}` (board-level read/write) + `pcb_io_kicad_sexpr_parser.{h,cpp}` (per-item S-expression parser).
  - `.kicad_sch` → `eeschema/sch_io/kicad_sexpr/`: `sch_io_kicad_sexpr.{h,cpp}` (sheet-level read/write), `sch_io_kicad_sexpr_common.{h,cpp}`, `sch_io_kicad_sexpr_parser.{h,cpp}` (per-item parser).
  - These per-item read/write functions are the de facto grammar (each item type maps to sexpr tokens); no standalone grammar doc exists.
- [x] **Guidance/glossary doc drafted:** `docs/autorouting_glossary.md` — terminology aligned to stages `ingest→parse→connectivity→route→drc→writeback`, routing task/failure modes, NP-completeness → heuristics, maze/Lee/global-routing + rip-up strategies, DRC via `kicad-cli pcb drc` JSON reports, and force-directed placement (spring attraction along nets, Coulomb repulsion, locked footprints pinned). Linked from top-level README **References** and indexed in `docs/README.md`. Also fixed stale "JSON file format" wording in `pipeline.py` parse-stage description → S-expression.
- [x] Downloaded Wikipedia research sources as local markdown under `docs/wikipedia/`: `autorouter.md` (actual article: "Routing (electronic design automation)"), `maze_routing.md` (redirects to "Maze runner" — verified this is the EDA maze-routing article, resolving the earlier title doubt), `lee_algorithm.md`, `force_directed_graph_drawing.md`. Fetched via en.wikipedia.org REST API + markdownify (no nav boilerplate). Excluded "Maze-routing algorithm" (general maze solving, not PCB routing). Indexed in `docs/README.md`.
- [x] Moved trial notebooks + `utilities.py` into `experiments/`; deleted generated artifacts; wrote `.gitignore`, `.env.example`.
- [x] Fixed `requirements.txt`: removed fake `kicad-cli` pip dep (system tool); kept numpy, networkx, pyyaml, pytest, black, mypy.
- [x] `src/kicad_autorouter/pipeline.py`: `Stage` dataclass + `STAGES` (`ingest`, `parse`, `connectivity`, `route`, `drc`, `writeback`; all `"planned"`) — single source of truth for UI.
- [x] `ui/server.py` (stdlib HTTP, `/api/stages`, path-traversal guard) + `ui/index.html` (dark workflow UI: stage cards + file drop). No new pip deps.
- [x] **File format confirmed:** `.kicad_pcb` / `.kicad_sch` are S-expressions, not JSON. Version tags `(version 20241229)` pcb, `(version 20250114)` sch → parser must be an S-expression reader.
- [x] `src/kicad_autorouter/validate.py`: wraps `kicad-cli pcb drc` / `sch erc --format json`; distinguishes load failures (exit 3) from rule violations; header check works without CLI.
- [x] Tests: `tests/test_validate.py` + fixtures in `tests/fixtures/` (EuroCard template). 9 pass; CLI tests skip if `kicad-cli` absent.
- [x] Saved KiCad 10 docs as markdown under `docs/kicad/`: `pcbnew.md`, `kicad.md`, `eeschema.md`, `cli.md` (from app-bundled HTML, exact version match). Wrote `docs/README.md` index.
- [x] `git init`; initial commit `65edcd8`. Identity: `kstammits` / `karl.stamm@gmail.com`.
- [x] Top-level `README.md`: overview, layout, run instructions, and a **References** section pointing guidance docs to `docs/` (keeps the human-facing README lean).
- [x] `src/kicad_autorouter/pcbnew_adapter.py`: only module importing `pcbnew`; `load_board()`, `unlocked_footprints()`, `nudge_unlocked_footprints()`, `add_track()`, `save_board()`.
- [x] `scripts/run_autoroute.py`: headless first-pass entry point (default nudge `--dx-mm 1.0` / `--dy-mm 0.0`; demo F.Cu track unless `--no-track`).
- [x] `.gitignore`: ignore generated outputs under `experiments/out/`.
- [x] **Baby-step first pass verified** under bundled Python: loaded 4 footprints, nudged unlocked `MH1` `(58.57,50.55)→(59.57,50.55)` mm, added F.Cu track `(80,95)→(135,95)` w `0.25`, saved `experiments/out/baby_step.kicad_pcb` (+ `.kicad_pro`). DRC loads clean with 1 expected `track_dangling | Track has unconnected end`; `pytest tests/ -q` → 9 pass.
- [x] **Pure-Python S-expression reader:** `src/kicad_autorouter/sexpr.py` — tokenizer + recursive-descent parser producing an immutable `SExpr` tree (`head` symbol + `args`, with `children()`/`find()`). Handles bare symbols, quoted strings (C-style escapes), ints/floats/negatives, and hex-with-underscore masks (`0x…`). No `pcbnew` import; runs in `.venv`. Role (2026-09-29 decision): portable structural reader / test double for headless & CI use — NOT the routing-accurate geometry source (that is pcbnew-backed via the adapter). Tests: `tests/test_sexpr.py` (unit cases + full parse of `tests/fixtures/minimal.kicad_pcb`); `pytest tests/ -q` → 19 pass; black + mypy clean.
- [x] **Pure-Python pcb+sch pair round trip:** serializer `sexpr.to_sexpr()` (bare words kept as a `Symbol` str-subclass so original quoting is restored on write) + `src/kicad_autorouter/io.py`: `load_pair()`, `no_op_edit()` (identity hook for future edits), `save_pair()`, `noop_roundtrip()`, `verify_noop()` (re-parse equality). `scripts/roundtrip_pair.py` runs load → no-op edit → new files → checks (tree equality + kicad-cli DRC/ERC); verified on the fixture pair: written files re-parse equal and pass DRC/ERC clean. Tests: `tests/test_io.py`; `pytest tests/ -q` → 33 pass; black + mypy clean.

## Blocked / Known Issues
- No official "file format" page in KiCad 10 manual — use the KiCad source parsers as the grammar reference instead (paths recorded under Completed).
- `kicad-cli` not on PATH; full path `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`. Handled by `validate.find_kicad_cli()`.
- Pipeline stage list (`ingest→parse→connectivity→route→drc→writeback`) is tentative, pending user review.

## Environment Notes
- Working dir: `/Users/karl/Workspace/KiCadAutoRouterLab`. `.venv/` exists (gitignored, do not delete); deps installed.
- `.env` = `ENV=DEV` only; gitignored, mirrored in `.env.example`. UI is stdlib-only on purpose.
- KiCad 10.0.7 at `/Applications/KiCad/`; bundled help in `KiCad.app/Contents/SharedSupport/help/en/`.
- **Scope relaxed:** a minimal read-modify-write pass (load → nudge unlocked parts → add demo track → save) is allowed and done. Full routing algorithm / net connectivity still pending; guidance-doc research now in progress.

## Repo Layout (current)
```
KiCadAutoRouterLab/
├── .env / .env.example / .gitignore / README.md / TODO.md / requirements.txt
├── docs/            # README.md index + kicad/{pcbnew,kicad,eeschema,cli}.md
├── experiments/     # old trial notebooks + utilities.py; out/ = generated boards (gitignored)
├── scripts/         # run_autoroute.py (headless first pass), roundtrip_pair.py (no-op pair round trip)
├── src/kicad_autorouter/   # __init__.py (0.1.0), pipeline.py, validate.py, sexpr.py, io.py, pcbnew_adapter.py
├── tests/           # conftest.py, fixtures/, test_validate.py, test_sexpr.py, test_io.py
└── ui/              # server.py (/api/stages), index.html
```
