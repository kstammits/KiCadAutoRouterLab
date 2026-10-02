# KiCad AutoRouter Lab — TODO / Session State

Goal: utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace placement.
Phase: **headless placement pass done** (.venv: parse → force-spring proposal → per-UUID writeback → optional DRC; v0 stub physics, real sim = PLAN item 2) + **pcb+sch no-op round trip done** (pure Python: load pair → zero-delta nudge → new files → checks) + **placement preview infra done** (`ui/` params panel → run → SVG proposal overlay). Guidance/glossary doc drafted (`docs/autorouting_glossary.md`); full autorouter logic still pending. System on **KiCad 10** (`kicad-cli` 10.0.7); local docs are KiCad 10. Board-viewer plan for `ui/` agreed (see Active).

## Active / Next Moves (in order)
- [x] **Per-pad force-spring placement with organic rotation** — simulate at pad level so through-hole components rotate naturally. Rigid constraints keep pads of same footprint together; footprint pose (x, y, angle) derived from pad positions after simulation. Supersedes the footprint-level sim in placement.py.
- [ ] **Force-spring placement for hundreds of components** — full plan in [`PLAN.md`](PLAN.md). Done: lock/UUID parsing (item 1), v0 stub runner + UI preview infra, writeback path + headless entry point (items 3–4), docs updates (item 6). Remaining: vectorized numpy sim (item 2), tests/benchmark (item 5).
- [ ] **Extend the autorouter beyond the baby step** — headless `.venv` only; the file is the interface (edit/lock in KiCad → save → run tool → review result). Additive two-layer routing with per-net-class width/clearance rules.
   - **Decisions 2026-10-01:** no KiCad plugin, no PCM packaging (both dropped); pcbnew demoted to *offline test oracle* — pure-Python sexpr parse is the runtime path, bundled Python runs only in tests (supersedes the runtime-semantics half of the 2026-09-29 decision: semantic labels like pad/via nets and layers are explicit file tokens); routing is additive (only unconnected pad pairs; existing tracks untouched); copper pair fixed at F.Cu + B.Cu.
   - Dropped from original scope: plugin entry point, `packaging/` PCM scaffold, SWIG compute host, "both interfaces" split, `model.py` seam (`BoardModel` stays the core contract).
   - Small steps (each = one small file/function; A+B first, then D→E critical path, C in parallel):
     - [ ] **A1. Parse `(net_class ...)`** → `NetClass` dataclass in `board_model.py` (`trace_width`, `clearance`, `via_diameter`, `via_drill`).
     - [ ] **A2. Nets→class mapping** (named membership + default fallback) on `BoardModel`.
     - [ ] **A3. Per-pad copper layer set** from the pad's own `(layers ...)` token (`_pad` currently takes only the footprint layer).
     - [ ] **A4. Fixture with net classes + vias** (DCCF has neither; small synthetic file or one of your real boards).
     - [ ] **B1. pcbnew dump script** — read-only, stdlib-only under bundled 3.9: absolute pad positions, nets, zones → JSON (`scripts/` or repurposed `pcbnew_adapter.py`).
     - [ ] **B2. Parity test** — sexpr parse vs pcbnew JSON on fixtures (validates the hand-rolled rotation transforms + A1).
     - [ ] **C. Placement on `BoardModel`** — item 1's numpy sim targets it directly; locked mask from `(locked ...)` token. No seam work needed.
     - [ ] **D1. Grid construction** — rasterize F.Cu/B.Cu at resolution derived from min clearance/width; block cells from courtyards/zones/keepouts/outline (pure function, unit-testable).
     - [ ] **D2. Per-class clearance inflation** on the grid (obstacle halos sized by net class).
     - [ ] **D3. Lee BFS for one pad pair** — 3D grid (x, y, layer) with via transitions + via cost; no incremental updates yet.
     - [ ] **D4. Sequential nets** — most-constrained first; routed copper + halo added to the grid after each net.
     - [ ] **D5. Rip-up-and-reroute retry loop** for failed nets (reorder/retry until success or budget exhausted).
     - [ ] **D6. Post-process** — collinear simplification, per-class widths, UUIDs → track/via list.
     - [ ] **E1. `io.py` track writer** — insert `(track ...)` items into the sexpr tree + round-trip test (write → reparse → diff).
     - [ ] **E2. `io.py` via writer** — same pattern for `(via ...)`.
     - [ ] **E3. Pipeline entry script** — parse → place → route → writeback → optional `kicad-cli drc` gate (enforces net-class rules natively).
- [x] **Web UI board viewer v1** (`ui/`) — load a `.kicad_pcb` and see it rendered as SVG in-browser (periodic snapshots) without opening KiCad. Decision: in-browser SVG viewer is the review surface; interactive editing (locking parts, params) stays in KiCad / CLI flags.
  - **Deviation from original plan (2026-09-30):** no `pcbnew` adapter needed — geometry comes from pure-Python `board_model.py` (footprints/pads/tracks/vias/zones/edge cuts), rendered server-side by new `src/kicad_autorouter/svg_render.py`. Runs under `.venv`; bundled Python not required.
  - API: `POST /api/load?name=<n>` (raw body) or `?path=<rel>` (traversal-guarded vs repo root); `GET /api/state` → `{loaded,name,version}`; `GET /api/board.svg?v=N`. In-memory versioned `BoardState`; client polls `/api/state` every 2 s and refetches the SVG on version change.
  - v1 draws: edge cuts, zones (keepouts dashed), courtyards, tracks, vias, pads (circle/rect by shape, B.Cu mirror rotation), ref text; deterministic golden-angle net colors; mm = SVG user units. Silkscreen/dimensions/drill marks skipped in v1.
  - Tests: `tests/test_svg_render.py`, `tests/test_server.py` (58 passing); live-verified with the DCCF fixture via curl.
- [ ] **File picker that finds the board pair.** Add a "Load board…" button (native file dialog) next to the drop zone: picking one half of a pair auto-loads its sibling (`foo.kicad_pcb` ↔ `foo.kicad_sch`, same basename). Drop should also match dropped files into pairs by basename; server-side `?path=` loads can pull the sibling from disk (repo-relative, traversal-guarded as today).
- [ ] **Web UI board viewer v2** — side-by-side before/after panes highlighting moved footprints + new tracks; optional DRC marker overlay from `validate.py` JSON.

## Bugs (board viewer)
- [ ] **No zoom/pan — boards larger than the viewport are unusable.** The board renders as a plain `<img>` (`ui/index.html:48`, capped at 60vh via `ui/index.html:22`); a big board is scaled down to fit and there's no way to zoom in or pan. Need interactive pan/zoom on the SVG (inline SVG + viewBox: wheel = zoom, drag = pan, plus a "fit" button).
- [ ] **Can't load a second board — drop zone disappears after first load.** Once a board loads, `#board` and `#placement-panel` render above `.drop`, pushing it below the fold (`ui/index.html:164-181`); there's no "load another / unload" control in the board bar and no server endpoint to clear state. Related: loading a new `.kicad_pcb` leaves the old `.kicad_sch` in `BoardState` (`ui/server.py:38-51`), so the sch chip can report a stale pair.

## Completed
- [x] **S-expression grammar reference identified** (via GitHub mirror `KiCad/kicad-source-mirror`, branch `master`; GitLab direct access is 403). Raw URL pattern: `https://raw.githubusercontent.com/KiCad/kicad-source-mirror/master/<path>`.
  - `.kicad_pcb` → `pcbnew/pcb_io/kicad_sexpr/`: `pcb_io_kicad_sexpr.{h,cpp}` (board-level read/write) + `pcb_io_kicad_sexpr_parser.{h,cpp}` (per-item S-expression parser).
  - `.kicad_sch` → `eeschema/sch_io/kicad_sexpr/`: `sch_io_kicad_sexpr.{h,cpp}` (sheet-level read/write), `sch_io_kicad_sexpr_common.{h,cpp}`, `sch_io_kicad_sexpr_parser.{h,cpp}` (per-item parser).
  - These per-item read/write functions are the de facto grammar (each item type maps to sexpr tokens); no standalone grammar doc exists.
- [x] `src/kicad_autorouter/validate.py`: wraps `kicad-cli pcb drc` / `sch erc --format json`; distinguishes load failures (exit 3) from rule violations; header check works without CLI.
- [x] Saved KiCad 10 docs as markdown under `docs/kicad/`: `pcbnew.md`, `kicad.md`, `eeschema.md`, `cli.md` (from app-bundled HTML, exact version match). Wrote `docs/README.md` index.
- [x] Top-level `README.md`: overview, layout, run instructions, and a **References** section pointing guidance docs to `docs/` (keeps the human-facing README lean).
- [x] `scripts/run_autoroute.py`: headless placement entry point (.venv: parse → `run_placement` → per-UUID writeback via `io` → save; optional `--validate` DRC). Replaces the old pcbnew nudge baby step (2026-10-01); tests in `tests/test_run_autoroute.py`.
- [x] `scripts/roundtrip_pair.py`: fixed — was broken by the UUID refactor (imported `noop_roundtrip`/`verify_noop` no longer in `io.py`); rewritten on the current API with a zero-delta nudge as the no-op edit (2026-10-01).

## Environment Notes
- KiCad 10.0.7 at `/Applications/KiCad/`; bundled help in `KiCad.app/Contents/SharedSupport/help/en/`.
