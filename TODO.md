# KiCad AutoRouter Lab — TODO / Session State

Goal: utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace placement.
Phase: **minimal read-modify-write pass done** (load board → nudge unlocked parts → add demo track → save) + **pcb+sch no-op round trip done** (pure Python: load pair → identity edit → new files → checks). Guidance/glossary doc drafted (`docs/autorouting_glossary.md`); full autorouter logic still pending. System on **KiCad 10** (`kicad-cli` 10.0.7); local docs are KiCad 10. Board-viewer plan for `ui/` agreed (see Active).

## Active / Next Moves (in order)
- [ ] **Extend the autorouter beyond the baby step.** Both interfaces, shared core; SWIG `pcbnew` runtime; PCM packaging = build target only (nothing to package yet).
   - Core stays pure Python (no `pcbnew` import) → testable in `.venv`. **Decision 2026-09-29:** parse-stage *semantics* — absolute/rotation-aware pad positions, nets, zones — are sourced from `pcbnew` via the adapter (`LoadBoard()` typed objects), NOT hand-rolled on top of `sexpr.py`; `sexpr.py` stays a portable structural reader / test double. New: `model.py` (abstract board fed by the adapter), `placement.py` (force-directed nudge, locked=fixed), `routing.py` (maze/Lee, later); `pcbnew_adapter.py` already exists (only place importing `pcbnew`).
  - Locked components = native footprint lock `fp.IsLocked()` / `fp.SetLocked(True)`; only unlocked parts move.
  - Runs under bundled Python 3.9: `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3` (verify numpy/networkx availability there).
  - Install dir: `~/Documents/KiCad/10.0/scripting/plugins/<name>/`. PCM scaffold later in `packaging/` (`metadata.json.template` v2 + `build_package.py` stub).
- [x] **Web UI board viewer v1** (`ui/`) — load a `.kicad_pcb` and see it rendered as SVG in-browser (periodic snapshots) without opening KiCad. Decision: in-browser SVG viewer is the review surface; interactive editing (locking parts, params) stays in KiCad / CLI flags.
  - **Deviation from original plan (2026-09-30):** no `pcbnew` adapter needed — geometry comes from pure-Python `board_model.py` (footprints/pads/tracks/vias/zones/edge cuts), rendered server-side by new `src/kicad_autorouter/svg_render.py`. Runs under `.venv`; bundled Python not required.
  - API: `POST /api/load?name=<n>` (raw body) or `?path=<rel>` (traversal-guarded vs repo root); `GET /api/state` → `{loaded,name,version}`; `GET /api/board.svg?v=N`. In-memory versioned `BoardState`; client polls `/api/state` every 2 s and refetches the SVG on version change.
  - v1 draws: edge cuts, zones (keepouts dashed), courtyards, tracks, vias, pads (circle/rect by shape, B.Cu mirror rotation), ref text; deterministic golden-angle net colors; mm = SVG user units. Silkscreen/dimensions/drill marks skipped in v1.
  - Tests: `tests/test_svg_render.py`, `tests/test_server.py` (58 passing); live-verified with the DCCF fixture via curl.
- [ ] **Web UI board viewer v2** — side-by-side before/after panes highlighting moved footprints + new tracks; optional DRC marker overlay from `validate.py` JSON.

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
