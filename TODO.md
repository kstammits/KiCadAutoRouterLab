# KiCad AutoRouter - TODO

## ⚠️ Session State Notes (2026-10-06, verified)

**Working tree batch** (not yet committed; branch is 2 commits ahead of origin):
per-region placement rewrite + shapely courtyard collision (`placement.py`), routing infra R1–R4/R7, new `src/kicad_autorouter/drc.py`, track rip-up (`io.rip_up_nets`, `board_model.commit_placement`) + `tests/test_track_ripup.py`, UI overhaul phases 1–4, pyproject.toml editable install, shapely in requirements.txt, start_server.sh browser-open.

**Test status:** 150 passed, 3 skipped — all green.
(Fixed 2026-10-05: `accept_proposal` unpacked `identify_power_nets` as 2 values and used list `|` union; now unpacks 3 and converts to sets. `ui/server.py` import is fine — `UI_DIR`/`REPO_ROOT` are defined at module level.)

### Half-wired features (UI controls exist, backend missing)
1. **DRC overlay:** UI has DRC toggle + `STATE.drc_violations`, but NO endpoint ever calls `set_drc_violations()` — no `/api/drc` handler exists. Overlay can never show anything. Backend should use existing `drc.run_drc_on_tree()`.
2. **Layer toggles (F.Cu/B.Cu):** server reads `fcu`/`bcu` query params (~lines 322–323) but never passes them to `render_board_svg`, which has no such parameters. Toggles do nothing.

### Courtyard collision (multi-board R3): ✅ COMPLETED 2026-10-06
- Shapely-based polygon collision implemented in `placement.py` (`_footprint_courtyard_polygon` at :400, collision in `_compute_total_force` at :654)
- **Fixed bugs:**
  - Stale geometry: polygons now rebuilt each iteration from current pad positions
  - O(n) lookup: added `fp_uuid_to_local_idx` dict replacing `fp_uuids.index()`
  - Per-footprint force distribution (not per-pad pair) creates natural torque
- `courtyard_repulsion_kc` updated to 2000.0 in both `placement.json` and `PlacementParams` default
- Tests: `test_courtyard_polygon_collision` + `test_courtyard_forces_are_zero_when_disabled` pass

### Housekeeping
- R3 numbering collision resolved: multi-board R3 = courtyard collision ✅, routing R3 = multi-net sequential (different sections)

## Completed ✅

### UI Overhaul (Phases 1-4)
- [x] Phase 1: Footprint Sidebar + List
  - [x] `/api/footprints` endpoint returning footprint data
  - [x] JavaScript fetchFootprints() on board load
  - [x] Render footprint table in sidebar tbody
  - [x] Search/filter: ref pattern, net, layer, state
  - [x] Selection sync: table ↔ SVG ↔ server
  - [x] Sidebar buttons: Select Visible, Clear, Pin, Unpin
  - [x] Resizable sidebar splitter with localStorage persistence

- [x] Phase 2: Better Action Buttons
  - [x] Replace "Step placement" with "Step Selected", "Step All Movable", "Preview"
  - [x] Updated click handlers for new buttons
  - [x] Preview: quick ghost positions (stub mode, 1 iteration)

- [x] Phase 3: Visual Polish & Net Legend
  - [x] Net legend bar with click-to-select-net
  - [x] SVG visual states: movable (green tint), locked (red dash), pinned (orange dash), selected (green solid)
  - [x] State badges in table rows

- [x] Phase 4: Advanced Selection
  - [x] Box/marquee drag selection on SVG
  - [x] Keyboard shortcuts (A=select all, Esc=clear, P=pin, U=unpin, Space=step)
  - [x] Net-based selection from legend click

### Parameter Sliders
- [x] Log-scale sliders for: repulsion_kr, attraction_ka, convergence_eps_mm, rigid_stiffness, boundary_repulsion_kb
- [x] Fixed `boundary_repulsion_kb` max (now 1M, default was 100k)
- [x] Added missing params: demo_jitter_mm (0-5mm), stub (checkbox)
- [x] Extended ranges: max_iterations to 20k, courtyard_repulsion_kc to 20k
- [x] Round proposal floats to 5 decimal places in JSON output

### Track Rip-Up on Accept
- [x] `rip_up_nets()` in io.py - removes segments/vias on affected nets from S-expression tree
- [x] `commit_placement()` in board_model.py - moves footprints + prunes tracks/vias
- [x] Updated `accept_proposal()` in server.py - applies to both BoardModel and PCB tree
- [x] Auto-detect protected nets (power/ground) via `identify_power_nets()`

### Test Coverage: Track Rip-Up
- [x] Core commit_placement Tests (4) in `tests/test_track_ripup.py`
- [x] S-expression rip_up_nets Tests (3)
- [x] Server Integration Tests (4)
- [x] Edge Cases (2)

### Infrastructure
- [x] `start_server.sh` now opens default browser on startup
- [x] Pipeline collapse toggle (▼/▶) to maximize board view
- [x] Rounding of proposal floats to 5 decimal places
- [x] **pyproject.toml** with editable install (`pip install -e .`)

### Multi-Board Region Support (R1-R5)
- [x] Board region detection (BoardRegion, _extract_board_polygons, _assign_board_regions)
- [x] Per-region force simulation in placement.py
- [x] Tests for all region functionality (10 tests) using DCCF + tube111

### Routing Infrastructure (R1-R4, R7)
- [x] Grid & Obstacle Infrastructure (`grid.py`, `obstacles.py`)
- [x] Single-Net Router with A* + MST (`router.py`)
- [x] Multi-Net Sequential + Rip-up (`scheduler.py`)
- [x] Placement Integration (`commit_placement`, `rip_up_nets`)
- [x] Output & Writeback (`output.py`, `apply_routes_to_tree`)

### KiCad PCB Version Detection
- [x] `get_generator_version()` in sexpr.py
- [x] `version` and `version_warning` fields in BoardModel
- [x] v8/v9/v10 load normally, v11+ warns "compatibility not verified"
- [x] `/api/load` and `/api/state` return `board_version` and `warning`

---

## In Progress / Next Session 📋

### Routing Completion (R4, R6, R8)
- [x] **R3. `placement.py`: Polygon courtyard collision (shapely).** ✅ COMPLETED 2026-10-06
  - `_footprint_courtyard_polygon(fp)`: build shapely Polygon from courtyard segments (arcs approximated), or pad bbox fallback
  - Overlap detection: `poly_i.intersects(poly_j)` → penetration vector from intersection centroid
  - Force: always-on 1/d² falloff; overlap → penetration_area * penetration_vector / (dist + eps); near-miss → margin / d²
  - Apply per-pad (creates natural torque); all footprints participate (pad bbox for no-courtyard)
  - Replaced stale `_compute_courtyard_forces` with polygon-based implementation

- [x] **R4. Parameter updates & dependency.** ✅ COMPLETED 2026-10-06
  - `placement.json`: `courtyard_repulsion_kc` 500 → 2000
  - `requirements.txt`: add `shapely>=2.0.0`

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
- [ ] **R8 (continued)**: `run_iterative_pipeline(model, params, max_cycles=5)`
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