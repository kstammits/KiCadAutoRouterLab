# KiCad AutoRouter Lab — TODO / Session State

Goal: utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace placement.
Phase: **minimal read-modify-write pass done** (load board → nudge unlocked parts → add demo track → save) + **pcb+sch no-op round trip done** (pure Python: load pair → identity edit → new files → checks). Guidance/glossary doc drafted (`docs/autorouting_glossary.md`); full autorouter logic still pending. System on **KiCad 10** (`kicad-cli` 10.0.7); local docs are KiCad 10. Board-viewer plan for `ui/` agreed (see Active).

## Active / Next Moves (in order)
- [ ] **Extend the autorouter beyond the baby step.** Both interfaces, shared core; SWIG `pcbnew` runtime; PCM packaging = build target only (nothing to package yet).
   - Core stays pure Python (no `pcbnew` import) → testable in `.venv`. **Decision 2026-09-29:** parse-stage *semantics* — absolute/rotation-aware pad positions, nets, zones — are sourced from `pcbnew` via the adapter (`LoadBoard()` typed objects), NOT hand-rolled on top of `sexpr.py`; `sexpr.py` stays a portable structural reader / test double. New: `model.py` (abstract board fed by the adapter), `placement.py` (force-directed nudge, locked=fixed), `routing.py` (maze/Lee, later); `pcbnew_adapter.py` already exists (only place importing `pcbnew`).
  - Locked components = native footprint lock `fp.IsLocked()` / `fp.SetLocked(True)`; only unlocked parts move.
  - Runs under bundled Python 3.9: `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3` (verify numpy/networkx availability there).
  - Install dir: `~/Documents/KiCad/10.0/scripting/plugins/<name>/`. PCM scaffold later in `packaging/` (`metadata.json.template` v2 + `build_package.py` stub).
- [ ] **Web UI board viewer** (`ui/`) — render `.kicad_pcb` in-browser to review routing progress without opening KiCad. Decision: in-browser SVG viewer is the review surface; interactive editing (locking parts, params) stays in KiCad / CLI flags. 
  - Data path: add `export_board_json(board)` to `src/kicad_autorouter/pcbnew_adapter.py` (only module importing `pcbnew`) → compact JSON in mm: bounds, layer list, footprints (ref, x/y, rotation, courtyard, pads via `GetAbsolutePosition()`), tracks (endpoints, width, layer), zone outlines. The adapter's pcbnew objects are the accurate semantic source. 
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
- [x] `src/kicad_autorouter/validate.py`: wraps `kicad-cli pcb drc` / `sch erc --format json`; distinguishes load failures (exit 3) from rule violations; header check works without CLI.
- [x] Saved KiCad 10 docs as markdown under `docs/kicad/`: `pcbnew.md`, `kicad.md`, `eeschema.md`, `cli.md` (from app-bundled HTML, exact version match). Wrote `docs/README.md` index.
- [x] Top-level `README.md`: overview, layout, run instructions, and a **References** section pointing guidance docs to `docs/` (keeps the human-facing README lean).
- [x] `scripts/run_autoroute.py`: headless first-pass entry point (default nudge `--dx-mm 1.0` / `--dy-mm 0.0`; demo F.Cu track unless `--no-track`).

## Environment Notes
- KiCad 10.0.7 at `/Applications/KiCad/`; bundled help in `KiCad.app/Contents/SharedSupport/help/en/`.
