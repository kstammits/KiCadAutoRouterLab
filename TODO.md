# KiCad AutoRouter - TODO

- [x] Move the Route/Place dialogs from the bottom to the right side as a pull-out panel (done: `ui/index.html` right-side `#side-panel` drawer, 2026-10-08).
- [x] Version number visible + bumped past 0.2 (done: top-bar `#app-version` chip via `/api/version`, now v0.3.0; `pyproject.toml` + `__init__.py` synced, 2026-10-08).

## ⚠️ Session State Notes (2026-10-08, verified)

**Working tree batch** (not yet committed; branch is 4 commits ahead of origin):
per-region placement rewrite + shapely courtyard collision (`placement.py`), routing infra R1–R4/R7, new `src/kicad_autorouter/drc.py`, track rip-up (`io.rip_up_nets`, `board_model.commit_placement`) + `tests/test_track_ripup.py`, UI overhaul phases 1–4, pyproject.toml editable install, shapely in requirements.txt, start_server.sh browser-open.
Plus this session: move-preservation fix (`apply_deltas`/`commit_placement` via `replace`) + `TestMovePreservation`, tube111 3-region asserts, pipeline stages → implemented, right-side `#side-panel` drawer, version 0.3.0 sync, `run-btn` null guard.

**Test status:** 239 passed, 12 skipped — all green (full suite 2026-10-08; `test_server.py` 14 passed after UI changes).
(Fixed 2026-10-05: `accept_proposal` unpacked `identify_power_nets` as 2 values and used list `|` union; now unpacks 3 and converts to sets. `ui/server.py` import is fine — `UI_DIR`/`REPO_ROOT` are defined at module level.)
(Fixed 2026-10-08: `apply_deltas` dropped `pad_type`/`drill_mm`/`layers`/`ghost`/`version`/regions on move; now uses `replace` throughout. `commit_placement` preserves board metadata via `replace(moved_model, tracks=…, vias=…)`.)

### Half-wired features (UI controls exist, backend incomplete)
1. **DRC overlay (needs UI trigger):** backend is wired (`POST /api/drc` runs kicad-cli, `GET /api/drc/status`, `set_drc_violations()`, svg `drc=` renders the cache) — but the UI never POSTs `/api/drc`, so the cache stays empty and the toggle shows nothing. Needs a "Run DRC" button or auto-run on toggle.
2. **Layer toggles (F.Cu/B.Cu):** checkboxes exist but have no listeners, `boardSrc()` doesn't send `fcu`/`bcu`, server parses them (`server.py` ~lines 640–641) yet never passes them to `render_board_svg`, which has no such parameters. Toggles do nothing.

## In Progress / Next Session 📋

### Routing Completion (R6, R8)
- [ ] **R6. Through-Hole "Free Via" Support** (partial)
  - THT pad marks cell blocked on BOTH layers ✅
  - Layer transition at THT pad = 0 via cost (pending: tht_via_mask in router)
  - Power/ground nets: THT pads become automatic stitching points (deferred)

- [ ] **R8. Tests & Parity**
  - Unit tests: grid transforms, obstacle marking, A* pathfinding, MST, RipUpManager
  - Integration test: route tube111 (pre-routed) → verify connectivity, THT free vias, no DRC violations
  - Parity test: our routes vs existing tube111 routes
  - DRC gate: route → writeback → `kicad-cli drc` → iterate

### Iterative Place/Route Cycle
- [ ] `run_iterative_pipeline(model, params, max_cycles=5)`
  - Place → get affected nets → rip up → route affected → repeat until converged

### Protected Nets Configuration
- [ ] Add "Protected nets" input field in placement panel (comma-separated)
- [ ] Persist protected nets in placement.json
- [ ] Pass protected nets from UI → `/api/placement/accept`
- [ ] Fallback: auto-detect + hardcoded if UI list empty

### Download Validation
- [ ] Downloaded PCB after accept loads in KiCad without DRC errors
- [ ] Integration test: accept → download → kicad-cli drc passes

### Server Test with Real Tracks
- [ ] Add test_accept_proposal_with_tracks using tube111 fixture (has tracks/nets)
- [ ] Verify version increments, proposal cleared, tracks reduced

### Performance Baseline
- [ ] Benchmark rip_up_nets on large boards (5000+ segments)
- [ ] Profile commit_placement on 200+ footprints

### Pad Model Enhancement ✅ (done 2026-10-08 — verified in tree, guarded by `TestMovePreservation`)
- [x] Add `pad_type`, `drill_mm`, `layers` fields to `Pad` dataclass
- [x] Parse `thru_hole`/`smd` from S-expression in `_pad()`
- [x] Add `is_through_hole` property (pad_type=="thru_hole" + drill>0 + *.Cu in layers)
- [x] Update `obstacles.py` to use `pad.is_through_hole` instead of shape check (`obstacles.py:101`)

---

## Future Enhancements 📋

### Selection/Workflow
- [ ] Range select in table (Shift+click for range)
- [ ] Filter by footprint library name
- [ ] Filter by component value (e.g., "10k", "0.1uF")
- [ ] Selection persistence across accepts/undos
- [ ] "Select by net" dropdown in toolbar

### Visual
- [ ] Highlight selected footprint in SVG on table hover
- [ ] Show force vectors as SVG overlay option
- [ ] Mini-map/overview panel
- [ ] Layer toggle (F.Cu / B.Cu / both)

### Routing Integration
- [x] "Route" button in UI (done: Route Selected / Route All in routing panel)
- [x] Show routing progress (done: loading spinner on buttons)
- [ ] DRC violations overlay (backend wired, UI trigger missing — see Half-wired)
- [ ] Writeback/download with routed tracks

### Performance
- [ ] Virtual scrolling for large footprint lists (500+)
- [ ] Debounced filter input
- [ ] Web Worker for placement simulation

### Persistence
- [ ] Save/load session state (selection, pinned, params)
- [ ] Project file (.kicar_autorouter) with UI state

---

## Notes

### Server Startup
```bash
./start_server.sh [port]  # defaults to 8000, opens browser
```

### Key Files Modified
- `ui/index.html` - Complete UI overhaul; right-side `#side-panel` drawer (2026-10-08)
- `ui/server.py` - New endpoints, accept logic, rip-up integration
- `src/kicad_autorouter/io.py` - `rip_up_nets()` function
- `src/kicad_autorouter/board_model.py` - `commit_placement()` function; move-preservation fix via `replace` (2026-10-08)
- `src/kicad_autorouter/svg_render.py` - Movable footprint styling
- `src/kicad_autorouter/sexpr.py` - Version extraction helpers
- `src/kicad_autorouter/placement.py` - Vectorized force-spring simulation
- `src/kicad_autorouter/routing/` - Grid, obstacles, router, scheduler, output, pipeline
- `src/kicad_autorouter/pipeline.py` - Stage statuses corrected to implemented (2026-10-08)
- `tests/test_magnet.py` - `TestMovePreservation` regression (2026-10-08)
- `tests/test_board_model.py`, `tests/test_placement.py` - tube111 3-region asserts (2026-10-08)
- `pyproject.toml`, `src/kicad_autorouter/__init__.py` - version 0.3.0 sync (2026-10-08)

### Protected Nets Logic
Auto-detected via `identify_power_nets()` + hardcoded fallbacks:
```python
protected = power_nets | ground_nets | {"GND", "GND_PWR", "VCC", "VDD", "VSS", "GROUND"}
```

### Accept Flow
1. Push undo state
2. Compute affected nets from moved footprints' pads
3. `commit_placement(model, deltas, protected)` → BoardModel with pruned tracks
4. `rip_up_nets(pcb_tree, affected, protected)` → S-expression without segments/vias
5. `nudge_footprint_by_uuid()` for each moved footprint
6. Clear proposal, increment version
