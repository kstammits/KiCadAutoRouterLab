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

### Half-wired features ✅ (wired 2026-10-08 — verified live on tube111)
1. **DRC overlay:** added "Run DRC" button (`POST /api/drc`) + auto-run when the toggle is switched on with an empty/stale cache; result counts shown in `#drc-info`. Verified: 28 violations + 4 unconnected, overlay renders with `drc=1`, absent with `drc=0`.
2. **Layer toggles (F.Cu/B.Cu):** fixed selector bug — `#layer-F.Cu-tracks` never matches (dot parses as class); now uses `[id="…"]` attribute selectors via `applyLayerVisibility()`, re-applied on every SVG inject. Toggles hide/show the layer's tracks+zones groups (vias share one group and stay visible).

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
  - Router fixes landed 2026-10-09 (found while removing demo jitter):
    - `obstacles._mark_zone` passed (col,row) to `_fill_polygon` which takes (row,col) → phantom full-height blocked bars; fixed + orientation regression test
    - Courtyard outlines were BLOCKED, trapping every enclosed SMD pad; now HIGH_COST (placement guides, not copper keepouts) + escapability regression test
    - `router._a_star` never recorded parent pointers → every route collapsed to its final step; fixed + full-chain regression test
  - Scores/widths landed 2026-10-09: `CostMap.heuristic_weight` 200→3 (penalties steer again), graded per-net fanout halo (+500/+100 secondary grid, own-net excluded, steers without trapping), `(net_class trace_width/clearance)` parsing + `RoutingParams.net_widths` override (params → file → heuristic → default), width-aware pad rings + emitted-width corridor reservation, `run_routing` hardcoded 0.25 + hardcoded via sizes fixed, duplicate-via dedupe. `test_drc_routing` DRC gate passes un-xfailed (3/3 nets, only lib/synthetic + single-pad-unconnected violations remain, both ignored).

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
- [x] Layer toggle (F.Cu / B.Cu / both) — done 2026-10-08 for tracks+zones (vias share one group)

### Routing Integration
- [x] "Route" button in UI (done: Route Selected / Route All in routing panel)
- [x] Show routing progress (done 2026-10-08: loading spinner; upgraded 2026-10-09 to per-net chunked loop — see below)
- [x] DRC violations overlay (done 2026-10-08: Run button + toggle auto-run + svg overlay)
- [ ] Writeback/download with routed tracks

### Chunked Routing UI (done 2026-10-09)
- UI routes ONE NET PER REQUEST (`routeSelectedNets` loop in `ui/index.html`) instead of a single batch POST: per-net progress bar (`#route-progress`), Cancel button (`#route-cancel-btn`, cooperative — stops after the in-flight net, routed nets kept), pre-flight pair-search estimate (`estimateRoutingWork`, `k*(k-1)/2` per net), per-net failure continuation (HTTP 400 aborts the batch — bad params would fail every net; other failures continue), incremental SVG refresh per net.
- DECISION: chunking over a server job model (background thread + poll + cancel) — chunking needs no server threading changes because each one-net call rebuilds grids from the current model, which already holds prior nets' copper. Server job model deferred until per-pair progress proves necessary.
- Equivalence: `tests/test_routing_chunked.py` — chunked sequential == batched (routed/failed sets identical; track totals within ±20% — see known rebuild tradeoff noted in `pipeline.route_nets` step 2 comment).
- KNOWN FOLLOW-UP: width-aware rebuilds — `obstacles._mark_track` marks centerline-only while `_apply_route_to_grid` reserves full corridor; chunked later nets run slightly tighter. Left as-is; belongs with GND-continuity work below.

### Routing Benchmarks (agreed 2026-10-09, not yet implemented)
- [ ] `scripts/benchmark_routing.py`: DCCF ladder (2/3/8-terminal nets + synthetic 20-terminal net), per-stage wall times + cProfile top-20 for one A*; anchors: occupancy ≈0.1s, 3-pad route ≈1s.
- [ ] Trivial cleanup only if profiling confirms free: drop redundant `cost_matrix` in `SingleNetRouter.route()` (byte-identical to `dist_matrix`), hoist `heapq` import.
- [ ] Structural scaling (heuristic-MST → A* on tree edges only) deferred; GND excluded from trace-speed investment (copper pours later).

### GND Continuity / Copper Pours (agreed 2026-10-09, phased)
- Current state: `Zone` parsed + drawn, but routing treats pours as pure `BLOCKED` obstacles (`obstacles._mark_zone`); `route_ground_stitching` only handles ≥2 same-layer zones via centroid routing — real fragmentation (signal tracks carving one pour into islands) is undetected.
- [ ] Phase 1 — fragmentation diagnostics: `routing/pour.py` `gnd_islands()` (connected components of GND copper per layer: zones + pads + tracks on the grid), `GET /api/routing/debug/gnd-islands`, UI overlay. No routing changes.
- [ ] Phase 2 — stitching-via suggestions (preview list, accept per-via/all; no auto track-moving).
- [ ] Phase 3 — auto-stitch + continuity-aware routing (research-grade; settle GND-as-traces vs pours first).

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
- 2026-10-09 (jitter/stub removal + step/run UX): `placement.py` (drop `demo_jitter_mm`/`stub`/`_jitter_for`, 1e-9 delta filter), `scripts/run_autoroute.py` (drop `--stub`/`--demo-jitter-mm`, step default 5), `ui/server.py` (replace-based iter override, reject empty accepts), `ui/index.html` (drop Physics/Preview/stub UI, step default 5, Run Selected/All), `placement.json` (drop jitter/stub keys), `routing/obstacles.py` (zone (row,col) fix, courtyard HIGH_COST), `routing/router.py` (A* parent pointers), `tests/test_drc_routing.py` (board-space courtyards, strict-xfail DRC gate)

### Protected Nets Logic
Auto-detected via `identify_power_nets()` + hardcoded fallbacks:
```python
protected = power_nets | ground_nets | {"GND", "GND_PWR", "VCC", "VDD", "VSS", "GROUND"}
```

### Accept Flow
1. Push undo state (skipped when the proposal is empty — server rejects it with 400)
2. Compute affected nets from moved footprints' pads
3. `commit_placement(model, deltas, protected)` → BoardModel with pruned tracks
4. `rip_up_nets(pcb_tree, affected, protected)` → S-expression without segments/vias
5. `nudge_footprint_by_uuid()` for each moved footprint
6. Clear proposal, increment version

### Placement Run Flow (2026-10-09)
- Demo jitter + stub mode removed entirely (they let net-free boards drift forever past Edge.Cuts). Physics is the only mode; net-free boards correctly propose no movement.
- UI: Step (default 5 iters) for previews, Run (saved max_iterations) to convergence; Accept stays disabled on empty proposals with a "no movement" message.
- Sub-mm float noise filtered (`1e-9`) so converged boards propose `{}`.
