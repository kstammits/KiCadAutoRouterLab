# KiCad AutoRouter - TODO

Can we move the Route/Place dialogs, which are currently on the bottom of the page, put them on the right side as a pull out panel? I have a wide-screen laptop and not much vertical space.
Also let's get a version number visible somewhere, and bump it. we should be past 0.2 so far.

## ⚠️ Session State Notes (2026-10-06, verified)

**Working tree batch** (not yet committed; branch is 2 commits ahead of origin):
per-region placement rewrite + shapely courtyard collision (`placement.py`), routing infra R1–R4/R7, new `src/kicad_autorouter/drc.py`, track rip-up (`io.rip_up_nets`, `board_model.commit_placement`) + `tests/test_track_ripup.py`, UI overhaul phases 1–4, pyproject.toml editable install, shapely in requirements.txt, start_server.sh browser-open.

**Test status:** 150 passed, 3 skipped — all green.
(Fixed 2026-10-05: `accept_proposal` unpacked `identify_power_nets` as 2 values and used list `|` union; now unpacks 3 and converts to sets. `ui/server.py` import is fine — `UI_DIR`/`REPO_ROOT` are defined at module level.)

### Half-wired features (UI controls exist, backend missing)
1. **DRC overlay:** UI has DRC toggle + `STATE.drc_violations`, but NO endpoint ever calls `set_drc_violations()` — no `/api/drc` handler exists. Overlay can never show anything. Backend should use existing `drc.run_drc_on_tree()`.
2. **Layer toggles (F.Cu/B.Cu):** server reads `fcu`/`bcu` query params (~lines 322–323) but never passes them to `render_board_svg`, which has no such parameters. Toggles do nothing.

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

### Pad Model Enhancement
- [ ] Add `pad_type`, `drill_mm`, `layers` fields to `Pad` dataclass
- [ ] Parse `thru_hole`/`smd` from S-expression in `_pad()`
- [ ] Add `is_through_hole` property (pad_type=="thru_hole" + drill>0 + *.Cu in layers)
- [ ] Update `obstacles.py` to use `pad.is_through_hole` instead of shape check

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
- [ ] "Route" button in UI (trigger routing pipeline)
- [ ] Show routing progress
- [ ] DRC violations overlay
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
- `ui/index.html` - Complete UI overhaul
- `ui/server.py` - New endpoints, accept logic, rip-up integration
- `src/kicad_autorouter/io.py` - `rip_up_nets()` function
- `src/kicad_autorouter/board_model.py` - `commit_placement()` function
- `src/kicad_autorouter/svg_render.py` - Movable footprint styling
- `src/kicad_autorouter/sexpr.py` - Version extraction helpers
- `src/kicad_autorouter/placement.py` - Vectorized force-spring simulation
- `src/kicad_autorouter/routing/` - Grid, obstacles, router, scheduler, output, pipeline

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
